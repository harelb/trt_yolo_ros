from __future__ import division, print_function

import os
import time

from PIL import Image, ImageDraw

import cv2
import numba
import numpy as np
import pycuda.autoinit
import pycuda.driver as cuda
import tensorrt as trt


class DarknetTRT(object):
    def __init__(
        self,
        trt_engine="yolov3.trt",
        onnx_engine="yolov3.onnx",
        obj_threshold=0.6,
        nms_threshold=0.7,
        h=608,
        w=608,
        label_file_path="coco_labels.txt",
        cuda_device=0,
        show_image=False,
    ):
        # input to the node node
        self.h = 0
        self.w = 0
        self.show_image = show_image
        # input to the model
        self.yolo_input_h = h
        self.yolo_input_w = w
        self.output_shapes = [(1, 255, 19, 19), (1, 255, 38, 38), (1, 255, 76, 76)]
        postprocessor_args = {
            "yolo_masks": [
                (6, 7, 8),
                (3, 4, 5),
                (0, 1, 2),
            ],  # A list of 3 three-dimensional tuples for the YOLO masks
            "yolo_anchors": [
                (10, 13),
                (16, 30),
                (33, 23),
                (30, 61),
                (62, 45),  # A list of 9 two-dimensional tuples for the YOLO anchors
                (59, 119),
                (116, 90),
                (156, 198),
                (373, 326),
            ],
            "obj_threshold": obj_threshold,  # Threshold for object coverage, float value between 0 and 1
            "nms_threshold": nms_threshold,  # Threshold for non-max suppression algorithm, float value between 0 and 1
            "yolo_input_resolution": (self.yolo_input_h, self.yolo_input_w),
        }
        self.postprocessor = PostprocessYOLO(**postprocessor_args)
        self.all_categories = [line.rstrip("\n") for line in open(label_file_path)]
        self.trt_logger = trt.Logger()
        self.engine = self.get_engine(onnx_engine, trt_engine)
        self.inputs, self.outputs, self.bindings, self.stream = self._allocate_buffers()

    def __call__(self, image):
        image, image_processed = self.image_preparation(image)
        shape_orig_WH = image.shape
        # Output shapes expected by the post-processor
        # Do inference with TensorRT
        trt_outputs = []
        with self.engine.create_execution_context() as context:
            self.inputs[0].host = image_processed
            trt_outputs = self.do_inference(
                context,
                bindings=self.bindings,
                inputs=self.inputs,
                outputs=self.outputs,
                stream=self.stream,
            )
        # Before doing post-processing, we need to reshape the outputs as the do_inference will give us flat arrays.
        trt_outputs = [
            output.reshape(shape)
            for output, shape in zip(trt_outputs, self.output_shapes)
        ]
        # Before doing post-processing, we need to reshape the outputs as the do_inference will give us flat arrays.
        # Run the post-processing algorithms on the TensorRT outputs and get the bounding box details of detected objects
        boxes, classes, scores = self.postprocessor.process(
            trt_outputs, (shape_orig_WH[:2])
        )
        # Draw the bounding boxes onto the original input image and save it as a PNG file
        obj_detected_img = None
        if self.show_image:
            obj_detected_img = self.draw_bboxes(
                image, boxes, scores, classes, self.all_categories
            )
        return boxes, classes, scores, obj_detected_img

    def get_engine(self, onnx_file_path, engine_file_path):
        """Attempts to load a serialized engine if available, otherwise builds a new TensorRT engine and saves it."""

        def build_engine(onnx_file_path):
            """Takes an ONNX file and creates a TensorRT engine to run inference with"""
            with trt.Builder(
                self.trt_logger
            ) as builder, builder.create_network() as network, trt.OnnxParser(
                network, self.trt_logger
            ) as parser:
                builder.max_workspace_size = 1 << 28  # 256MiB
                builder.max_batch_size = 1
                # Parse model file
                if not os.path.exists(onnx_file_path):
                    print(
                        "ONNX file {} not found, please run yolov3_to_onnx.py first to generate it.".format(
                            onnx_file_path
                        )
                    )
                    exit(1)
                print("Loading ONNX file from path {}...".format(onnx_file_path))
                with open(onnx_file_path, "rb") as model:
                    print("Beginning ONNX file parsing")
                    parser.parse(model.read())
                print("Completed parsing of ONNX file")
                print(
                    "Building an engine from file {}; this may take a while...".format(
                        onnx_file_path
                    )
                )
                engine = builder.build_cuda_engine(network)
                print("Completed creating Engine")
                with open(engine_file_path, "wb") as f:
                    f.write(engine.serialize())
                exit(1)

        if os.path.exists(engine_file_path):
            # If a serialized engine exists, use it instead of building an engine.
            print("Reading engine from file {}".format(engine_file_path))
            with open(engine_file_path, "rb") as f, trt.Runtime(
                self.trt_logger
            ) as runtime:
                return runtime.deserialize_cuda_engine(f.read())
        else:
            build_engine(onnx_file_path)

    def image_preparation(self, img_raw):
        """ Extract image and shape """
        height, width, channels = img_raw.shape

        if (height != self.h) or (width != self.w):
            self.h = height
            self.w = width

            # Determine image to be used
            self.padded_image = np.zeros(
                (max(self.h, self.w), max(self.h, self.w), channels)
            ).astype(float)

        # Add padding
        if self.w > self.h:
            self.padded_image[
                (self.w - self.h) // 2 : self.h + (self.w - self.h) // 2, :, :
            ] = img_raw
        else:
            self.padded_image[
                :, (self.h - self.w) // 2 : self.w + (self.h - self.w) // 2, :
            ] = img_raw

        # Resize and normalize
        input_img = (
            cv2.resize(self.padded_image, (self.yolo_input_h, self.yolo_input_w))
            / 255.0
        )
        input_img = np.array(input_img, dtype=np.float32, order="C")
        # HWC to CHW format:
        input_img = np.transpose(input_img, [2, 0, 1])
        # CHW to NCHW format
        input_img = np.expand_dims(input_img, axis=0)
        # Convert the input_img to row-major order, also known as "C order":
        input_img = np.array(input_img, dtype=np.float32, order="C")
        return img_raw, input_img

    def draw_bboxes(
        self,
        image_raw,
        bboxes,
        confidences,
        categories,
        all_categories,
        bbox_color="blue",
    ):
        """Draw the bounding boxes on the original input image and return it.

        Keyword arguments:
        image_raw -- a raw PIL Image
        bboxes -- NumPy array containing the bounding box coordinates of N objects, with shape (N,4).
        categories -- NumPy array containing the corresponding category for each object,
        with shape (N,)
        confidences -- NumPy array containing the corresponding confidence for each object,
        with shape (N,)
        all_categories -- a list of all categories in the correct ordered (required for looking up
        the category name)
        bbox_color -- an optional string specifying the color of the bounding boxes (default: 'blue')
        """
        draw = ImageDraw.Draw(Image.fromarray(image_raw))
        if bboxes is None:
            return np.array(image_raw)
        for box, score, category in zip(bboxes, confidences, categories):
            x_coord, y_coord, width, height = box
            left = max(0, np.floor(x_coord + 0.5).astype(int))
            top = max(0, np.floor(y_coord + 0.5).astype(int))
            right = min(image_raw.shape[1], np.floor(x_coord + width + 0.5).astype(int))
            bottom = min(
                image_raw.shape[0], np.floor(y_coord + height + 0.5).astype(int)
            )

            draw.rectangle(((left, top), (right, bottom)), outline=bbox_color)
            draw.text(
                (left, top - 12),
                "{0} {1:.2f}".format(all_categories[category], score),
                fill=bbox_color,
            )

        return np.array(image_raw)


# Simple helper data class that's a little nicer to use than a 2-tuple.
class HostDeviceMem(object):
    def __init__(self, host_mem, device_mem):
        self.host = host_mem
        self.device = device_mem

    def __str__(self):
        return "Host:\n" + str(self.host) + "\nDevice:\n" + str(self.device)

    def __repr__(self):
        return self.__str__()


class PostprocessYOLO(object):
    """Class for post-processing the three outputs tensors from YOLOv3-608."""

    def __init__(
        self,
        yolo_masks,
        yolo_anchors,
        obj_threshold,
        nms_threshold,
        yolo_input_resolution,
    ):
        """Initialize with all values that will be kept when processing several frames.
        Assuming 3 outputs of the network in the case of (large) YOLOv3.

        Keyword arguments:
        yolo_masks -- a list of 3 three-dimensional tuples for the YOLO masks
        yolo_anchors -- a list of 9 two-dimensional tuples for the YOLO anchors
        object_threshold -- threshold for object coverage, float value between 0 and 1
        nms_threshold -- threshold for non-max suppression algorithm,
        float value between 0 and 1
        input_resolution_yolo -- two-dimensional tuple with the target network's (spatial)
        input resolution in HW order
        """
        self.masks = yolo_masks
        self.anchors = yolo_anchors
        self.object_threshold = obj_threshold
        self.nms_threshold = nms_threshold
        self.input_resolution_yolo = yolo_input_resolution

    def process(self, outputs, resolution_raw):
        """Take the YOLOv3 outputs generated from a TensorRT forward pass, post-process them
        and return a list of bounding boxes for detected object together with their category
        and their confidences in separate lists.

        Keyword arguments:
        outputs -- outputs from a TensorRT engine in NCHW format
        resolution_raw -- the original spatial resolution from the input PIL image in WH order
        """
        outputs_reshaped = list()
        for output in outputs:
            outputs_reshaped.append(self._reshape_output(output))

        boxes, categories, confidences = self._process_yolo_output(
            outputs_reshaped, resolution_raw
        )

        return boxes, categories, confidences

    def _reshape_output(self, output):
        """Reshape a TensorRT output from NCHW to NHWC format (with expected C=255),
        and then return it in (height,width,3,85) dimensionality after further reshaping.

        Keyword argument:
        output -- an output from a TensorRT engine after inference
        """
        output = np.transpose(output, [0, 2, 3, 1])
        _, height, width, _ = output.shape
        dim1, dim2 = height, width
        dim3 = 3
        # There are CATEGORY_NUM=80 object categories:
        dim4 = 4 + 1 + 80
        return np.reshape(output, (dim1, dim2, dim3, dim4))

    def _process_yolo_output(self, outputs_reshaped, resolution_raw):
        """Take in a list of three reshaped YOLO outputs in (height,width,3,85) shape and return
        return a list of bounding boxes for detected object together with their category and their
        confidences in separate lists.

        Keyword arguments:
        outputs_reshaped -- list of three reshaped YOLO outputs as NumPy arrays
        with shape (height,width,3,85)
        resolution_raw -- the original spatial resolution from the input PIL image in WH order
        """

        # E.g. in YOLOv3-608, there are three output tensors, which we associate with their
        # respective masks. Then we iterate through all output-mask pairs and generate candidates
        # for bounding boxes, their corresponding category predictions and their confidences:
        boxes, categories, confidences = list(), list(), list()
        for output, mask in zip(outputs_reshaped, self.masks):
            box, category, confidence = self._process_feats(output, mask)
            box, category, confidence = self._filter_boxes(box, category, confidence)
            boxes.append(box)
            categories.append(category)
            confidences.append(confidence)

        boxes = np.concatenate(boxes)
        categories = np.concatenate(categories)
        confidences = np.concatenate(confidences)

        # Scale boxes back to original image shape:
        width, height = resolution_raw
        image_dims = [width, height, width, height]
        boxes = boxes * image_dims

        # Using the candidates from the previous (loop) step, we apply the non-max suppression
        # algorithm that clusters adjacent bounding boxes to a single bounding box:
        nms_boxes, nms_categories, nscores = list(), list(), list()
        for category in set(categories):
            idxs = np.where(categories == category)
            box = boxes[idxs]
            category = categories[idxs]
            confidence = confidences[idxs]

            keep = self._nms_boxes(box, confidence)

            nms_boxes.append(box[keep])
            nms_categories.append(category[keep])
            nscores.append(confidence[keep])

        if not nms_categories and not nscores:
            return None, None, None

        boxes = np.concatenate(nms_boxes)
        categories = np.concatenate(nms_categories)
        confidences = np.concatenate(nscores)

        return boxes, categories, confidences

    @numba.jit()
    def _process_feats(self, output_reshaped, mask):
        """Take in a reshaped YOLO output in height,width,3,85 format together with its
        corresponding YOLO mask and return the detected bounding boxes, the confidence,
        and the class probability in each cell/pixel.

        Keyword arguments:
        output_reshaped -- reshaped YOLO output as NumPy arrays with shape (height,width,3,85)
        mask -- 2-dimensional tuple with mask specification for this output
        """

        grid_h, grid_w, _, _ = output_reshaped.shape

        anchors = [self.anchors[i] for i in mask]

        # Reshape to N, height, width, num_anchors, box_params:
        anchors_tensor = np.reshape(anchors, [1, 1, len(anchors), 2])
        box_xy = 1.0 / (1.0 + np.exp(-output_reshaped[..., :2]))
        box_wh = 1.0 / (1.0 + np.exp(-output_reshaped[..., 2:4])) * anchors_tensor
        box_confidence = 1.0 / (1.0 + np.exp(-output_reshaped[..., 4]))

        box_confidence = np.expand_dims(box_confidence, axis=-1)
        box_class_probs = 1.0 / (1.0 + np.exp(-output_reshaped[..., 5:]))

        col = np.tile(np.arange(0, grid_w), grid_w).reshape(-1, grid_w)
        row = np.tile(np.arange(0, grid_h).reshape(-1, 1), grid_h)

        col = col.reshape(grid_h, grid_w, 1, 1).repeat(3, axis=-2)
        row = row.reshape(grid_h, grid_w, 1, 1).repeat(3, axis=-2)
        grid = np.concatenate((col, row), axis=-1)

        box_xy += grid
        box_xy /= (grid_w, grid_h)
        box_wh /= self.input_resolution_yolo
        box_xy -= box_wh / 2.0
        boxes = np.concatenate((box_xy, box_wh), axis=-1)

        # boxes: centroids, box_confidence: confidence level, box_class_probs:
        # class confidence
        return boxes, box_confidence, box_class_probs

    def _filter_boxes(self, boxes, box_confidences, box_class_probs):
        """Take in the unfiltered bounding box descriptors and discard each cell
        whose score is lower than the object threshold set during class initialization.

        Keyword arguments:
        boxes -- bounding box coordinates with shape (height,width,3,4); 4 for
        x,y,height,width coordinates of the boxes
        box_confidences -- bounding box confidences with shape (height,width,3,1); 1 for as
        confidence scalar per element
        box_class_probs -- class probabilities with shape (height,width,3,CATEGORY_NUM)

        """
        box_scores = box_confidences * box_class_probs
        box_classes = np.argmax(box_scores, axis=-1)
        box_class_scores = np.max(box_scores, axis=-1)
        pos = np.where(box_class_scores >= self.object_threshold)

        boxes = boxes[pos]
        classes = box_classes[pos]
        scores = box_class_scores[pos]

        return boxes, classes, scores

    def _nms_boxes(self, boxes, box_confidences):
        """Apply the Non-Maximum Suppression (NMS) algorithm on the bounding boxes with their
        confidence scores and return an array with the indexes of the bounding boxes we want to
        keep (and display later).

        Keyword arguments:
        boxes -- a NumPy array containing N bounding-box coordinates that survived filtering,
        with shape (N,4); 4 for x,y,height,width coordinates of the boxes
        box_confidences -- a Numpy array containing the corresponding confidences with shape N
        """
        x_coord = boxes[:, 0]
        y_coord = boxes[:, 1]
        width = boxes[:, 2]
        height = boxes[:, 3]

        areas = width * height
        ordered = box_confidences.argsort()[::-1]

        keep = list()
        while ordered.size > 0:
            # Index of the current element:
            i = ordered[0]
            keep.append(i)
            xx1 = np.maximum(x_coord[i], x_coord[ordered[1:]])
            yy1 = np.maximum(y_coord[i], y_coord[ordered[1:]])
            xx2 = np.minimum(
                x_coord[i] + width[i], x_coord[ordered[1:]] + width[ordered[1:]]
            )
            yy2 = np.minimum(
                y_coord[i] + height[i], y_coord[ordered[1:]] + height[ordered[1:]]
            )

            width1 = np.maximum(0.0, xx2 - xx1 + 1)
            height1 = np.maximum(0.0, yy2 - yy1 + 1)
            intersection = width1 * height1
            union = areas[i] + areas[ordered[1:]] - intersection

            # Compute the Intersection over Union (IoU) score:
            iou = intersection / union

            # The goal of the NMS algorithm is to reduce the number of adjacent bounding-box
            # candidates to a minimum. In this step, we keep only those elements whose overlap
            # with the current bounding box is lower than the threshold:
            indexes = np.where(iou <= self.nms_threshold)[0]
            ordered = ordered[indexes + 1]

        keep = np.array(keep)
        return keep