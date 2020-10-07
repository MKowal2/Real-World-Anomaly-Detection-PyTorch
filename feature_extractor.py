import argparse
import os
from os import path, mkdir
import numpy as np
import torch
import torch.backends.cudnn as cudnn
from tqdm import tqdm
from data_loader import VideoIter
from network.c3d import C3D
from network.resnet import generate_model as resnet
from utils.utils import set_logger, build_transforms

parser = argparse.ArgumentParser(description="PyTorch Video Classification Parser")
# debug
parser.add_argument('--debug-mode', type=bool, default=True,
					help="print all setting for debugging.")
# io
# parser.add_argument('--dataset_path', default='/media/ssd1/m3kowal/UCF_Crimes/', help="path to dataset")
parser.add_argument('--dataset_path', default='/mnt/zeta_share_1/m3kowal/UCF_Crimes/Videos/', help="path to dataset of MP4 videos")
# parser.add_argument('--dataset_path', default='/mnt/zeta_share_1/m3kowal/UCF_Crimes/test_videos/', help="path to dataset of MP4 videos")
parser.add_argument('--annotation_path', default='/mnt/zeta_share_1/m3kowal/UCF_Crimes/Anomaly_Train.txt', help="path to annotations")
# parser.add_argument('--annotation_path', default='/mnt/zeta_share_1/m3kowal/UCF_Crimes/Anomaly_Test.txt', help="path to annotations")
parser.add_argument('--clip-length', type=int, default=16,
					help="define the length of each input sample.")
parser.add_argument('--num_workers', type=int, default=4,
					help="define the number of workers used for loading the videos")
parser.add_argument('--frame-interval', type=int, default=1,
					help="define the sampling interval between frames.")
parser.add_argument('--log-file', type=str, default="",
					help="set logging file.")
parser.add_argument('--save_dir', type=str, default="/mnt/zeta_share_1/m3kowal/UCF_Crimes/3dr_features",	help="set logging file.")

# device
parser.add_argument('--pretrained_c3d', type=str, default='/mnt/zeta_share_1/m3kowal/AnomalyDetectionCVPR2018-Pytorch/network/c3d.pickle', help="load default C3D pretrained model.")
parser.add_argument('--pretrained_3dresnet', type=str, default='/mnt/zeta_share_1/m3kowal/AnomalyDetectionCVPR2018-Pytorch/network/r3d200_K_200ep.pth', help="load default 3D ResNet pretrained model.")
parser.add_argument('--feature_extractor', type=str, default='c3d', help="network used for feature extraction")
parser.add_argument('--resume-epoch', type=int, default=-1,
					help="resume train")
# optimization
parser.add_argument('--batch-size', type=int, default=1,
					help="batch size")
parser.add_argument('--random-seed', type=int, default=1,
					help='random seed (default: 1)')


current_path = None
current_dir = None
current_data = None


class FeaturesWriter:
	def __init__(self, chunk_size=16):
		self.path = None
		self.dir = None
		self.data = None
		self.chunk_size = chunk_size

	def _init_video(self, video_name, dir):
		self.path = path.join(dir, f"{video_name}.txt")
		self.dir = dir
		self.data = dict()

	def has_video(self):
		return self.data is not None

	def dump(self):
		print(f'Dumping {self.path}')
		if not path.exists(self.dir):
			os.mkdir(self.dir)

		features = np.array([self.data[key] for key in sorted(self.data)])
		features = features / np.expand_dims(np.linalg.norm(features, ord=2, axis=-1), axis=-1)
		padding_count = int(32 * np.ceil(features.shape[0] / 32) - features.shape[0])
		features = torch.from_numpy(np.vstack([features, torch.zeros(padding_count, features.shape[1])]))
		segments = torch.stack(torch.chunk(features, chunks=32, dim=0))
		avg_segments = segments.mean(dim=-2).numpy()
		with open(self.path, 'w') as fp:
			for d in avg_segments:
				d = [str(x) for x in d]
				fp.write(' '.join(d) + '\n')

	def _is_new_video(self, video_name, dir):
		new_path = path.join(dir, f"{video_name}.txt")
		if self.path != new_path and self.path is not None:
			return True

		return False

	def store(self, feature, idx):
		self.data[idx] = list(feature)

	def write(self, feature, video_name, idx, dir):
		if not self.has_video():
			self._init_video(video_name, dir)

		if self._is_new_video(video_name, dir):
			self.dump()
			self._init_video(video_name, dir)

		self.store(feature, idx)


def read_features(video_name, dir):
	file_path = f"{video_name}.txt"
	file_path = path.join(dir, file_path)
	if not path.exists(file_path):
		raise Exception(f"Feature doesn't exist: {file_path}")
	features = None
	with open(file_path, 'r') as fp:
		data = fp.read().splitlines(keepends=False)
		try:
			features = np.zeros((len(data), 4096))
		except:
			features = np.zeros((len(data), 2048))
		for i, line in enumerate(data):
			features[i, :] = [float(x) for x in line.split(' ')]

	return torch.from_numpy(features)


def main():
	device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

	args = parser.parse_args()
	set_logger(log_file=args.log_file, debug_mode=args.debug_mode)

	torch.manual_seed(args.random_seed)
	torch.cuda.manual_seed(args.random_seed)
	cudnn.benchmark = True

	train_loader = VideoIter(dataset_path=args.dataset_path,
							annotation_path=args.annotation_path,
							clip_length=args.clip_length,
							frame_stride=args.frame_interval,
							video_transform=build_transforms(),
							name='Features extraction')

	train_iter = torch.utils.data.DataLoader(train_loader,
											batch_size=args.batch_size,
											shuffle=False,
											num_workers=args.num_workers,  # 4, # change this part accordingly
											pin_memory=True)

	# Loading network
	if args.feature_extractor == 'c3d':
		network = C3D(pretrained=args.pretrained_c3d)
	elif args.feature_extractor == 'resnet':
		network = resnet(200)
	network.load_state_dict(torch.load('network/r3d200_K_200ep.pth')['state_dict'])
	network = network.to(device)

	if not path.exists(args.save_dir):
		mkdir(args.save_dir)

	features_writer = FeaturesWriter()

	for i_batch, (data, target, sampled_idx, dirs, vid_names) in tqdm(enumerate(train_iter)):
		with torch.no_grad():
			outputs = network(data.to(device)).detach().cpu().numpy()

			for i, (dir, vid_name, start_frame) in enumerate(zip(dirs, vid_names, sampled_idx.cpu().numpy())):
				dir = path.join(args.save_dir, dir)
				features_writer.write(feature=outputs[i], video_name=vid_name, idx=start_frame, dir=dir)

	features_writer.dump()


if __name__ == "__main__":
	main()
