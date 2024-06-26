import logging
import os
import cv2
import torch
from copy import deepcopy
import torch.nn.functional as F
from torchvision.transforms import ToTensor
import math

from alnet import ALNet
from soft_detect import DKD
import time

configs = {
    'alike-t': {'c1': 8, 'c2': 16, 'c3': 32, 'c4': 64, 'dim': 64, 'single_head': True, 'radius': 2,
                'model_path': os.path.join(os.path.split(__file__)[0], 'models', 'alike-t.pth')},
    'alike-s': {'c1': 8, 'c2': 16, 'c3': 48, 'c4': 96, 'dim': 96, 'single_head': True, 'radius': 2,
                'model_path': os.path.join(os.path.split(__file__)[0], 'models', 'alike-s.pth')},
    'alike-n': {'c1': 16, 'c2': 32, 'c3': 64, 'c4': 128, 'dim': 128, 'single_head': True, 'radius': 2,
                'model_path': os.path.join(os.path.split(__file__)[0], 'models', 'alike-n.pth')},
    'alike-l': {'c1': 32, 'c2': 64, 'c3': 128, 'c4': 128, 'dim': 128, 'single_head': False, 'radius': 2,
                'model_path': os.path.join(os.path.split(__file__)[0], 'models', 'alike-l.pth')},
}


class ALike(ALNet):
    def __init__(self,
                 # ================================== feature encoder
                 c1: int = 32, c2: int = 64, c3: int = 128, c4: int = 128, dim: int = 128,
                 single_head: bool = False,
                 # ================================== detect parameters
                 radius: int = 2,
                 top_k: int = 500, scores_th: float = 0.5,
                 n_limit: int = 5000,
                 device: str = 'cpu',
                 model_path: str = ''
                 ):
        super().__init__(c1, c2, c3, c4, dim, single_head)
        self.radius = radius
        self.top_k = top_k
        self.n_limit = n_limit
        self.scores_th = scores_th
        self.dkd = DKD(radius=self.radius, top_k=self.top_k,
                       scores_th=self.scores_th, n_limit=self.n_limit)
        self.device = device
        self.time_all = 0



        self.time_dkd = 0
        self.time_normalization = 0
        self.time_convertion = 0

        if model_path != '':
            state_dict = torch.load(model_path, self.device)
            self.load_state_dict(state_dict)
            self.to(self.device)
            self.eval()
            logging.info(f'Loaded model parameters from {model_path}')
            logging.info(
                f"Number of model parameters: {sum(p.numel() for p in self.parameters() if p.requires_grad) / 1e3}KB")

    def extract_dense_map(self, image, ret_dict=False):
        # ====================================================
        # check image size, should be integer multiples of 2^5
        # if it is not a integer multiples of 2^5, padding zeros
        device = image.device
        b, c, h, w = image.shape
        h_ = math.ceil(h / 32) * 32 if h % 32 != 0 else h
        w_ = math.ceil(w / 32) * 32 if w % 32 != 0 else w
        if h_ != h:
            h_padding = torch.zeros(b, c, h_ - h, w, device=device)
            image = torch.cat([image, h_padding], dim=2)
        if w_ != w:
            w_padding = torch.zeros(b, c, h_, w_ - w, device=device)
            image = torch.cat([image, w_padding], dim=3)
        # ====================================================
        scores_map, descriptor_map = super().forward(image)
        # ====================================================
        if h_ != h or w_ != w:
            descriptor_map = descriptor_map[:, :, :h, :w]
            scores_map = scores_map[:, :, :h, :w]  # Bx1xHxW
        # ====================================================
        # BxCxHxW
        descriptor_map = torch.nn.functional.normalize(descriptor_map, p=2, dim=1)

        if ret_dict:
            return {'descriptor_map': descriptor_map, 'scores_map': scores_map, }
        else:
            return descriptor_map, scores_map
    
    def forward(self, scores_map, descriptor_map):
        """
        :param img: np.array HxWx3, RGB
        :param image_size_max: maximum image size, otherwise, the image will be resized
        :param sort: sort keypoints by scores
        :param sub_pixel: whether to use sub-pixel accuracy
        :return: a dictionary with 'keypoints', 'descriptors', 'scores', and 'time'
        """
        # descriptor_map = torch.tensor(descriptor_map)
        # scores_map = torch.tensor(scores_map)
        # print("Score map: ")
        # print(scores_map.shape)

        # print("Descriptor map: ")
        # print(descriptor_map.shape)
        H = 480
        W = 640

        
        time_start = time.time()
        descriptor_map =torch.from_numpy(descriptor_map)
        scores_map =torch.from_numpy(scores_map)
        self.time_convertion += time.time() - time_start


        time_normalization_start = time.time()
        descriptor_map = torch.nn.functional.normalize(descriptor_map, p=2, dim=1)
        self.time_normalization += time.time() - time_normalization_start




        sort=False
        sub_pixel=False
        self.time_all += time.time() - time_start



        time_start = time.time()
        keypoints, descriptors, scores, _ = self.dkd(scores_map, descriptor_map,
                                                         sub_pixel=sub_pixel)
        self.time_dkd += time.time() - time_start



        keypoints, descriptors, scores = keypoints[0], descriptors[0], scores[0]
        keypoints = (keypoints + 1) / 2 * keypoints.new_tensor([[W - 1, H - 1]])

        if sort:
            indices = torch.argsort(scores, descending=True)
            keypoints = keypoints[indices]
            descriptors = descriptors[indices]
            scores = scores[indices]

        
        return {'keypoints': keypoints.cpu().numpy(),
                'descriptors': descriptors.cpu().numpy(),
                'scores': scores.cpu().numpy(),
                'scores_map': scores_map.cpu().numpy()}

    def access_dkd(self):
        return self.dkd
if __name__ == '__main__':
    import numpy as np
    from thop import profile

    net = ALike(c1=32, c2=64, c3=128, c4=128, dim=128, single_head=False)

    image = np.random.random((640, 480, 3)).astype(np.float32)
    flops, params = profile(net, inputs=(image, 9999, False), verbose=False)
    print('{:<30}  {:<8} GFLops'.format('Computational complexity: ', flops / 1e9))
    print('{:<30}  {:<8} KB'.format('Number of parameters: ', params / 1e3))
