#!/usr/bin/env python

import roslib
import rospy
import cv2
import sys
import numpy as np
import threading

from offloadable_fr_node import Offloadable_FR_Node
from cv_bridge import CvBridge, CvBridgeError
from sensor_msgs.msg import Image
from offloadable_face_recognition.msg import FaceBox,SchedulerCommand, MotorCommand
from offloadable_face_recognition.srv import *

class LK_Tracker(Offloadable_FR_Node):

	def __init__(self, node_name):

		Offloadable_FR_Node.__init__(self, node_name)

		self.pyramid = None
		self.prev_pyramid = None
		self.grey = None
		self.prev_grey = None
		face_box = None
		self.features = []
		self.night_mode = False       
		self.WIN_SIZE = 10
		self.USE_HARRIS = False
		self.FLAGS = 0
		self.MAX_LEVEL = 2
		self.CV_FILLED = -1
		self.BAD_CLUSTER = -1
		self.COLOUR_FACE_BOX = (0,0,255) # BLUE
		self.COLOUR_FEATURE_POINTS = (0,255,0) # GREEN
		self.COLOUR_NO_FACE_TEXT = (255,0,0) # RED

		self.motor_commands = "motor_commands"

		self.camera_threshold_tolerance = 30 # %percent
		self.camera_x, self.camera_y = self.camera_dimensions
		self.camera_edge_threshold = self.camera_x/100*self.camera_threshold_tolerance

		self.abs_min_features = 6
		self.min_features = 5
		self.NO_IMAGE_TEXT = "NO FACE DETECTED!"
		self.face_box_lock = threading.Lock()
		self.subscriber_lock = threading.Lock()
		self.face_box = None
		self.feature_matrix = None
		self.marker_image = None
		self.pre_processed_image = None

		self.isOffloaded = True

		# # params for ShiTomasi corner detection
		# self.feature_params = dict( maxCorners = self.MAX_COUNT,
		#                        qualityLevel = self.QUALITY,
		#                        minDistance = self.GOOD_FEATURE_DISTANCE,
		#                        blockSize = self.BLOCK_SIZE,
		#                        k = 0.04)

		# Parameters for lucas kande optical flow
		self.lk_params = dict( winSize  = (self.WIN_SIZE,self.WIN_SIZE),
						  maxLevel = self.MAX_LEVEL,
						  criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 10, 0.03))
		
		self.scheduler_sub = rospy.Subscriber(self.scheduler_commands, SchedulerCommand, self.scheduler_listener, queue_size=self.queue_size)
		# self.output_image_pub = None
		# self.image_sub = None
		# self.face_box_sub = None

		# A publisher to output the display image back to a ROS topic 
		self.output_image_pub = rospy.Publisher(self.output_image, Image, queue_size=self.queue_size)

		# # Subscribe to the raw camera image topic and set the image processing callback 
		self.image_sub = rospy.Subscriber(self.face_detect_output_image, Image, self.track_lk, queue_size=self.queue_size)
		self.face_box_sub = rospy.Subscriber(self.face_box_coordinates, FaceBox, self.update_face_box, queue_size=self.queue_size)

		# Subscribe to the raw camera image topic and set the image processing callback to self.pre_processing()
		# image_sub = rospy.Subscriber(self.input_rgb_image, Image, self.pre_processing, queue_size=self.queue_size)
		# Subscribe to the preprocessed image output and set the detect_face as the callback
		#image_sub = rospy.Subscriber(self.marker_image_output, Image, self.update_marker_image, queue_size=self.queue_size)

	def update_face_box(self, face_box):
		with self.face_box_lock:
			self.face_box = face_box

	def track_lk(self, ros_image):

		cv_image = self.convert_img_to_cv(ros_image)
		im_width, im_height = cv_image.shape

		with self.face_box_lock:
			face_box = self.face_box

		# Switch between the incoming image streams depending on whether we have features or not
		if len(self.features) > 0 and self.isOffloaded == False:
			with self.subscriber_lock:
				self.image_sub.unregister()
				self.image_sub = rospy.Subscriber(self.pre_processed_output_image, Image, self.track_lk, queue_size = self.queue_size)
				self.face_box_sub.unregister()
				self.isOffloaded = True
				print "switched topic to tracking only"

		elif self.isOffloaded == True and len(self.features) == 0:
			with self.subscriber_lock:
				self.image_sub.unregister()
				self.image_sub = rospy.Subscriber(self.face_detect_output_image, Image, self.track_lk, queue_size=self.queue_size)
				#self.face_box_sub.unregister()
				self.face_box_sub = rospy.Subscriber(self.face_box_coordinates, FaceBox, self.update_face_box, queue_size=self.queue_size)

				self.isOffloaded = False
				print "switched topic back to face detector"

		feature_box = None
		
		#  Initialize intermediate images if necessary 
		if self.grey is None:
			self.grey = np.zeros((im_width,im_height,1), np.uint8)
			self.prev_grey = np.zeros((im_width,im_height,1), np.uint8)
			self.features = []
		
		self.marker_image = np.zeros((im_width,im_height,3), np.uint8)
		self.grey = cv_image

		if face_box and len(self.features) > 0:
			self.features, status, track_error = cv2.calcOpticalFlowPyrLK(self.prev_grey, self.grey, np.asarray(self.features,dtype="float32"), None, **self.lk_params)

			#  Keep only high status points 
			self.features = [ p for (st,p) in zip(status, self.features) if st]  

		elif face_box:
			self.features = self.add_features(ros_image, face_box, self.features)
			# Since the detect box is larger than the actual face or desired patch, shrink the number of features by 10% 
			#self.min_features = int(len(self.features) * 0.9)
			#self.abs_min_features = int(0.5 * self.min_features)
		# Swapping the images 
		self.prev_grey, self.grey = self.grey, self.prev_grey
		
		# If we have some features... 
		if len(self.features) > 0:
			# The FitEllipse2 function below requires us to convert the feature array into a CvMat matrix 
			# Draw the best fit ellipse around the feature points 
			if len(self.features) > self.min_features:
				self.feature_matrix = np.float32([p for p in self.features]).reshape(-1, 1, 2)  
				feature_box = cv2.fitEllipse(self.feature_matrix)
			else:
				feature_box = None

			self.features, score = self.prune_features(self.features)

			if score == self.BAD_CLUSTER:
				self.features = []
				self.detect_box = None
				face_box = None

		if (len(self.features) < self.abs_min_features) and (feature_box is not None):
			self.expand_roi = self.expand_roi_init * self.expand_roi
			((face_box.x, face_box.y), (face_box.width, face_box.height), a) = feature_box
			self.features = self.add_features(ros_image, face_box, self.features)
		else:
			self.expand_roi = self.expand_roi_init

		cv_image = cv2.cvtColor(cv_image, cv2.COLOR_GRAY2BGR)
		self.update_motor_position(self.features)
		cv_image = self.draw_graphics(cv_image, face_box, self.features)
		ros_image = self.convert_cv_to_img(cv_image, encoding="bgr8")

		if feature_box is not None and len(self.features) > 0:
			try:
				self.output_image_pub.publish(ros_image)
			except CvBridgeError, e:
				print e


	#Should move this into the motor controller and simply have
	#the motor controller recieve the current position?
	def update_motor_position(self, features):
		# take the center of the current ellipse as the mean point
		# and sends the data to the motor controller

		# Compute the COG (center of gravity) of the cluster 
		if len(features > 0):
			for point in features:
				sum_x = sum_x + point[0]
				sum_y = sum_y + point[1]
			
			center_x = sum_x / features_len
			center_y = sum_y / features_len

			left_threshold = self.camera_edge_threshold
			right_threshold = self.camera_x - self.camera_threshold_tolerance
			
			motor_command = MotorCommand()

			if center_x <= left_threshold:
				motor_command.command = self.YAW_RIGHT
				motor_command.angle = 20
				self.motor_commands.publish(motor_command)
			elif center_x >= right_threshold:
				motor_command.command = self.YAW_LEFT
				motor_command.angle = 20
				self.motor_commands.publish(motor_command)
			else:
				print "Face was within limits"


	def prune_features(self, prev_features):
		# takes an array of feature coordinates and prunes them
		# returning the new set of features and a quality score.
		# Prune features that are too far from the main cluster
			rospy.wait_for_service('prune_features')
			prune_features = rospy.ServiceProxy('prune_features', PruneFeatures)

			try:
				response = prune_features(self.convert_to_feature_coordinates(prev_features))

				features = self.convert_to_tuple_array(response.features)
				score = response.score

			except rospy.ServiceException as exc:
				self.features = []
				score = self.BAD_CLUSTER
				print("Service did not process request: " + str(exc))
					# Add features if the number is getting too low

			return features, score

	def add_features(self, ros_image, face_box, prev_features):
		# takes an image, a track box and an array of feature coordinates
		# and adds new features to this. Should return the array of 
		# new feature coordinates
		rospy.wait_for_service('add_features')
		add_features = rospy.ServiceProxy('add_features', AddFeatures)

		try:
			service_response = add_features(self.convert_to_feature_coordinates(prev_features), face_box, ros_image)
			features = self.convert_to_tuple_array(service_response.features)
		except rospy.ServiceException as exc:
			features = []
			print("Service did not process request: " + str(exc))

		return features

	def draw_graphics(self, cv_image, face_box, features):
		# take input image, check for whether there is a facebox or feature box
		# and if there is either, draw the appropriate graphics ontop of the
		# cv_image. Otherwise simply return the initial image. Returns feature matrix
		# Draw the points as green circles and add them to the features matrix 

		# If there is a face box then draw a rectange around the region the face occupies
		if face_box:
			pt1 = (face_box.x, face_box.y)
			pt2 = (face_box.x+face_box.width, face_box.y+face_box.height)
			cv2.rectangle(cv_image, pt1, pt2, self.COLOUR_FACE_BOX, thickness=2)

		# Otherwise if there are already features then draw the feature points as points on the face
		elif len(features) > 0:
			i = 0
			for the_point in features:
				cv2.circle(cv_image, (int(the_point[0]), int(the_point[1])), 1, self.COLOUR_FEATURE_POINTS,self.CV_FILLED)
				# try:
				# 	feature_matrix[0][i] = (int(the_point[0]), int(the_point[1]))
				# except:
				# 	pass
				# i = i + 1

		# else:
		# 	text_scale = 0.4 * im_width / 160. + 0.1
		# 	text_pos = (100, im_height-100)
		# 	cv2.putText(cv_image, self.NO_IMAGE_TEXT, text_pos, cv2.FONT_HERSHEY_COMPLEX, text_scale, self.COLOUR_NO_FACE_TEXT, thickness=4)


		return cv_image

	def unsubscribe_node(self):
		# Function to unsubscribe a node from its topics and stop publishing data
		with self.subscriber_lock:
			self.output_image_pub.unregister()
			self.image_sub.unregister()
			self.face_box_sub.unregister()

	def resubscribe_node(self):
		# Function to resubscribe and republish the nodes data
		with self.subscriber_lock:
			self.output_image_pub = rospy.Publisher(self.output_image, Image, queue_size=self.queue_size)
			self.image_sub = rospy.Subscriber(self.face_detect_output_image, Image, self.track_lk, queue_size=self.queue_size)
			self.face_box_sub = rospy.Subscriber(self.face_box_coordinates, FaceBox, self.update_face_box, queue_size=self.queue_size)
			self.motor_commands_pub = rospy.Publisher(self.motor_commands, MotorCommand, queue_size=self.queue_size)

def main(args):
	try:   
		# Fire up the node.
		LK = LK_Tracker("lk_tracker_node")
		# Spin so our services will work
		print "Node started..."
		rospy.spin()
	except KeyboardInterrupt:
		print "Shutting down vision node."
		cv.DestroyAllWindows()

if __name__ == '__main__':

	main(sys.argv)