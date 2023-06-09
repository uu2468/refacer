import cv2
import onnxruntime as rt
import sys
from insightface.app import FaceAnalysis
sys.path.insert(1, './recognition')
from scrfd import SCRFD
from arcface_onnx import ArcFaceONNX
import os.path as osp
import os
from pathlib import Path
from tqdm import tqdm
import ffmpeg
import random
import multiprocessing as mp
from concurrent.futures import ThreadPoolExecutor
from insightface.model_zoo.inswapper import INSwapper
import psutil
from enum import Enum
from insightface.app.common import Face
from insightface.utils.storage import ensure_available
import re
import subprocess
import numpy as np
from esrgan_onnx import ESRGAN

class RefacerMode(Enum):
     CPU, CUDA, COREML, TENSORRT = range(1, 5)

class Refacer:
    def __init__(self,force_cpu=False,colab_performance=False):
        self.first_face = False
        self.force_cpu = force_cpu
        self.colab_performance = colab_performance
        self.__check_encoders()
        self.__check_providers()
        self.total_mem = psutil.virtual_memory().total
        self.__init_apps()

    def __check_providers(self):
        if self.force_cpu :
            self.providers = ['CPUExecutionProvider']
        else:
            self.providers = rt.get_available_providers()
        rt.set_default_logger_severity(4)
        self.sess_options = rt.SessionOptions()
        self.sess_options.execution_mode = rt.ExecutionMode.ORT_SEQUENTIAL
        self.sess_options.graph_optimization_level = rt.GraphOptimizationLevel.ORT_ENABLE_ALL

        if len(self.providers) == 1 and 'CPUExecutionProvider' in self.providers:
            self.mode = RefacerMode.CPU
            self.use_num_cpus = mp.cpu_count()-1
            self.sess_options.intra_op_num_threads = int(self.use_num_cpus/3)
            print(f"CPU mode with providers {self.providers}")
        elif self.colab_performance:
            self.mode = RefacerMode.TENSORRT
            self.use_num_cpus = mp.cpu_count()-1
            self.sess_options.intra_op_num_threads = int(self.use_num_cpus/3)
            print(f"TENSORRT mode with providers {self.providers}")
        elif 'CoreMLExecutionProvider' in self.providers:
            self.mode = RefacerMode.COREML
            self.use_num_cpus = mp.cpu_count()-1
            self.sess_options.intra_op_num_threads = int(self.use_num_cpus/3)
            print(f"CoreML mode with providers {self.providers}")
        elif 'CUDAExecutionProvider' in self.providers:
            self.mode = RefacerMode.CUDA
            self.use_num_cpus = 1
            self.sess_options.intra_op_num_threads = 1
            if 'TensorrtExecutionProvider' in self.providers:
                self.providers.remove('TensorrtExecutionProvider')
            print(f"CUDA mode with providers {self.providers}")
        """
        elif 'TensorrtExecutionProvider' in self.providers:
            self.mode = RefacerMode.TENSORRT
            #self.use_num_cpus = 1
            #self.sess_options.intra_op_num_threads = 1
            self.use_num_cpus = mp.cpu_count()-1
            self.sess_options.intra_op_num_threads = int(self.use_num_cpus/3)
            print(f"TENSORRT mode with providers {self.providers}")
        """
        

    def __init_apps(self):
        assets_dir = ensure_available('models', 'buffalo_l', root='~/.insightface')

        model_path = os.path.join(assets_dir, 'det_10g.onnx')
        sess_face = rt.InferenceSession(model_path, self.sess_options, providers=self.providers)
        self.face_detector = SCRFD(model_path,sess_face)
        self.face_detector.prepare(0,input_size=(640, 640))

        model_path = os.path.join(assets_dir , 'w600k_r50.onnx')
        sess_rec = rt.InferenceSession(model_path, self.sess_options, providers=self.providers)
        self.rec_app = ArcFaceONNX(model_path,sess_rec)
        self.rec_app.prepare(0)

        model_path = 'inswapper_128.onnx'
        sess_swap = rt.InferenceSession(model_path, self.sess_options, providers=self.providers)
        self.face_swapper = INSwapper(model_path,sess_swap)
        self.face_swapper_input_size = self.face_swapper.input_size[0]
        #print("INSwapper resolution = ",self.face_swapper_input_size)


    def prepare_faces(self, faces):
        self.replacement_faces=[]
        for face in faces:
            #image1 = cv2.imread(face.origin)
            if "origin" in face:
                face_threshold = face['threshold']
                bboxes1, kpss1 = self.face_detector.autodetect(face['origin'], max_num=1)  
                if len(kpss1)<1:
                    raise Exception('No face detected on "Face to replace" image')
                feat_original = self.rec_app.get(face['origin'], kpss1[0])
            else:
                face_threshold = 0
                self.first_face = True
                feat_original = None
                print('No origin image: First face change')
            #image2 = cv2.imread(face.destination)
            _faces = self.__get_faces(face['destination'],max_num=1)
            if len(_faces)<1:
                raise Exception('No face detected on "Destination face" image')
            self.replacement_faces.append((feat_original,_faces[0],face_threshold))

    def __convert_video(self,video_path,output_video_path):
        if self.video_has_audio:
            print("Merging audio with the refaced video...")
            new_path = output_video_path + str(random.randint(0,999)) + "_c.mp4"
            #stream = ffmpeg.input(output_video_path)
            in1 = ffmpeg.input(output_video_path)
            in2 = ffmpeg.input(video_path)
            out = ffmpeg.output(in1.video, in2.audio, new_path,video_bitrate=self.ffmpeg_video_bitrate,vcodec=self.ffmpeg_video_encoder)
            out.run(overwrite_output=True,quiet=True)
        else:
            new_path = output_video_path
            print("The video doesn't have audio, so post-processing is not necessary")
        
        print(f"The process has finished.\nThe refaced video can be found at {os.path.abspath(new_path)}")
        return new_path

    def __get_faces(self,frame,max_num=0):

        bboxes, kpss = self.face_detector.detect(frame,max_num=max_num,metric='default')

        if bboxes.shape[0] == 0:
            return []
        ret = []
        for i in range(bboxes.shape[0]):
            bbox = bboxes[i, 0:4]
            det_score = bboxes[i, 4]
            kps = None
            if kpss is not None:
                kps = kpss[i]
            face = Face(bbox=bbox, kps=kps, det_score=det_score)
            face.embedding = self.rec_app.get(frame, kps)
            ret.append(face)
        return ret

    def paste_upscale(self, bgr_fake, M, img):
        bgr_fake_upscaled, self.scale_factor = self.esrgan_model.get(bgr_fake)
        M = M * self.scale_factor
        bgr_fake = cv2.resize(bgr_fake, (self.face_swapper_input_size*self.scale_factor, 
                                         self.face_swapper_input_size*self.scale_factor), interpolation = cv2.INTER_LINEAR )
        target_img = img
        aimg = cv2.warpAffine(img, M, (self.face_swapper_input_size*self.scale_factor, 
                                       self.face_swapper_input_size*self.scale_factor), borderValue=0.0)
        fake_diff = bgr_fake.astype(np.float32) - aimg.astype(np.float32)
        fake_diff = np.abs(fake_diff).mean(axis=2)
        fake_diff[:2,:] = 0
        fake_diff[-2:,:] = 0
        fake_diff[:,:2] = 0
        fake_diff[:,-2:] = 0
        IM = cv2.invertAffineTransform(M)
        img_white = np.full((aimg.shape[0],aimg.shape[1]), 255, dtype=np.float32)
        bgr_fake = cv2.warpAffine(bgr_fake_upscaled, IM, (target_img.shape[1], target_img.shape[0]), borderValue=0.0)
        img_white = cv2.warpAffine(img_white, IM, (target_img.shape[1], target_img.shape[0]), borderValue=0.0)
        fake_diff = cv2.warpAffine(fake_diff, IM, (target_img.shape[1], target_img.shape[0]), borderValue=0.0)
        img_white[img_white>20] = 255
        fthresh = 10
        fake_diff[fake_diff<fthresh] = 0
        fake_diff[fake_diff>=fthresh] = 255
        img_mask = img_white
        mask_h_inds, mask_w_inds = np.where(img_mask==255)
        mask_h = np.max(mask_h_inds) - np.min(mask_h_inds)
        mask_w = np.max(mask_w_inds) - np.min(mask_w_inds)
        mask_size = int(np.sqrt(mask_h*mask_w))
        k = max(mask_size//10, 10)
        #k = max(mask_size//20, 6)
        #k = 6
        kernel = np.ones((k,k),np.uint8)
        img_mask = cv2.erode(img_mask,kernel,iterations = 1)
        kernel = np.ones((2,2),np.uint8)
        fake_diff = cv2.dilate(fake_diff,kernel,iterations = 1)
        k = max(mask_size//20, 5)
        #k = 3
        #k = 3
        kernel_size = (k, k)
        blur_size = tuple(2*i+1 for i in kernel_size)
        img_mask = cv2.GaussianBlur(img_mask, blur_size, 0)
        k = 5
        kernel_size = (k, k)
        blur_size = tuple(2*i+1 for i in kernel_size)
        fake_diff = cv2.GaussianBlur(fake_diff, blur_size, 0)
        img_mask /= 255
        fake_diff /= 255
        #img_mask = fake_diff
        img_mask = np.reshape(img_mask, [img_mask.shape[0],img_mask.shape[1],1])
        fake_merged = img_mask * bgr_fake + (1-img_mask) * target_img.astype(np.float32)
        fake_merged = fake_merged.astype(np.uint8)
        return fake_merged

    def process_faces(self,frame):
        max_num=0
        if self.first_face:
            max_num=1
        
        faces = self.__get_faces(frame,max_num=max_num)
        for face in faces:
            if self.first_face:
                if not self.upscale_en: 
                    #print('\nRun native paste_back')
                    frame = self.face_swapper.get(frame, face, self.replacement_faces[0][1], paste_back=True)
                else: 
                    #print('\nRun upscale')
                    bgr_fake, M = self.face_swapper.get(frame, face, self.replacement_faces[0][1], paste_back=False)
                    frame = self.paste_upscale(bgr_fake,M,frame)
                break
            else:
                for rep_face in self.replacement_faces:
                    sim = self.rec_app.compute_sim(rep_face[0], face.embedding)
                    if sim>=rep_face[2]:
                        if not self.upscale_en: 
                            #print('\nRun native paste_back')
                            frame = self.face_swapper.get(frame, face, rep_face[1], paste_back=True)
                        else: 
                            #print('\nRun upscale')
                            bgr_fake, M = self.face_swapper.get(frame, face, rep_face[1], paste_back=False)
                            frame = self.paste_upscale(bgr_fake,M,frame)
        return frame

    def __check_video_has_audio(self,video_path):
        self.video_has_audio = False
        probe = ffmpeg.probe(video_path)
        audio_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'audio'), None)
        if audio_stream is not None:
            self.video_has_audio = True
        
    def reface(self, video_path, faces, upscaler):
        self.upscale_en = False
        self.upscaler_model=upscaler
        if upscaler != 'None': 
            self.upscale_en = True
            model_path = osp.join('models_ESRGAN',self.upscaler_model)
            sess_upsk = rt.InferenceSession(model_path, self.sess_options, providers=self.providers)
            self.esrgan_model = ESRGAN(sess_upsk)
        
        self.__check_video_has_audio(video_path)
        output_video_path = os.path.join('out',Path(video_path).name)
        self.prepare_faces(faces)

        cap = cv2.VideoCapture(video_path)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        print(f"Total frames: {total_frames}")
        
        fps = cap.get(cv2.CAP_PROP_FPS)
        frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        output = cv2.VideoWriter(output_video_path, fourcc, fps, (frame_width, frame_height))
        
        frames=[]
        self.k = 1
        with tqdm(total=total_frames,desc="Extracting frames") as pbar:
            while cap.isOpened():
                flag, frame = cap.read()
                if flag and len(frame)>0:
                    frames.append(frame.copy())
                    pbar.update()
                else:
                    break
            cap.release()
            pbar.close()
        
        with ThreadPoolExecutor(max_workers = self.use_num_cpus) as executor:
            results = list(tqdm(executor.map(self.process_faces, frames), total=len(frames),desc="Processing frames"))
            for result in results:
                output.write(result)
            output.release()

        return self.__convert_video(video_path,output_video_path)
    
    def __try_ffmpeg_encoder(self, vcodec):
        print(f"Trying FFMPEG {vcodec} encoder")
        command = ['ffmpeg', '-y', '-f','lavfi','-i','testsrc=duration=1:size=1280x720:rate=30','-vcodec',vcodec,'testsrc.mp4']
        try:
            subprocess.run(command, check=True, capture_output=True).stderr
        except subprocess.CalledProcessError as e:
            print(f"FFMPEG {vcodec} encoder doesn't work -> Disabled.")
            return False
        print(f"FFMPEG {vcodec} encoder works")
        return True
        
    def __check_encoders(self):
        self.ffmpeg_video_encoder='libx264'
        self.ffmpeg_video_bitrate='0'

        pattern = r"encoders: ([a-zA-Z0-9_]+(?: [a-zA-Z0-9_]+)*)"
        command = ['ffmpeg', '-codecs', '--list-encoders']
        commandout = subprocess.run(command, check=True, capture_output=True).stdout
        result = commandout.decode('utf-8').split('\n')
        for r in result:
            if "264" in r: 
                encoders = re.search(pattern, r).group(1).split(' ')
                for v_c in Refacer.VIDEO_CODECS:
                    for v_k in encoders:
                        if v_c == v_k:
                            if self.__try_ffmpeg_encoder(v_k):
                                self.ffmpeg_video_encoder=v_k
                                self.ffmpeg_video_bitrate=Refacer.VIDEO_CODECS[v_k]
                                print(f"Video codec for FFMPEG: {self.ffmpeg_video_encoder}")
                                return

    VIDEO_CODECS = {
         'h264_videotoolbox':'0', #osx HW acceleration
         'h264_nvenc':'0', #NVIDIA HW acceleration
         #'h264_qsv', #Intel HW acceleration
         #'h264_vaapi', #Intel HW acceleration
         #'h264_omx', #HW acceleration
         'libx264':'0' #No HW acceleration
    }