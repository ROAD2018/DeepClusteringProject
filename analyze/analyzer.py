from colorsys import hsv_to_rgb
from copy import deepcopy as dcp
from pathlib import Path
from typing import Tuple, Dict

import PIL
import numpy as np
import torch
from PIL import Image
from deepclustering import ModelMode
from deepclustering.arch import weights_init
from deepclustering.augment import pil_augment
from deepclustering.loss import KL_div
from deepclustering.meters import MeterInterface, ConfusionMatrix, AverageValueMeter
from deepclustering.model import Model
from deepclustering.utils import tqdm_, nice_dict, class2one_hot
from deepclustering.utils.classification.assignment_mapping import hungarian_match, flat_acc
from deepclustering.writer import DrawCSV2
from torch import Tensor, nn
from torch.utils.data import DataLoader, TensorDataset

from trainer import ClusteringGeneralTrainer


class LinearNet(nn.Module):

    def __init__(self, num_features, num_classes):
        super().__init__()
        self.fc = nn.Linear(num_features, num_classes)

    def forward(self, input):
        return self.fc(input)


class AnalyzeInference(ClusteringGeneralTrainer):
    checkpoint_identifier = "best.pth"

    def __init__(self, model: Model, train_loader_A: DataLoader, train_loader_B: DataLoader, val_loader: DataLoader,
                 criterion: nn.Module = nn.CrossEntropyLoss(), max_epoch: int = 100,
                 save_dir: str = "AnalyzerTrainer",
                 checkpoint_path: str = None, device="cpu", head_control_params: Dict[str, int] = {"B": 1},
                 use_sobel: bool = False, config: dict = None, **kwargs) -> None:
        super().__init__(model, train_loader_A, train_loader_B, val_loader, criterion, max_epoch, save_dir,
                         checkpoint_path, device, head_control_params, use_sobel, config, **kwargs)

        assert self.checkpoint, "checkpoint must be provided in `AnalyzeInference`."

    # for 10 point projection
    def save_plot(self, temporature=1) -> None:
        """
        using IIC method to show the evaluation, only for MNIST dataset, code taken largely from the original repo.
        """
        from .utils import Temporature

        headBheads = dcp(self.model.torchnet.head_B.heads)

        new_headBheads = nn.ModuleList()
        for head in headBheads:
            new_headBheads.append(nn.Sequential(*[Temporature(temporature), *head]))

        self.model.torchnet.head_B.heads = new_headBheads

        def get_coord(probs, num_classes):
            # computes coordinate for 1 sample based on probability distribution over c
            coords_total = np.zeros(2, dtype=np.float32)
            probs_sum = probs.sum()

            fst_angle = 0.

            for c in range(num_classes):
                # compute x, y coordinates
                coords = np.ones(2) * 2 * np.pi * (float(c) / num_classes) + fst_angle
                coords[0] = np.sin(coords[0])
                coords[1] = np.cos(coords[1])
                coords_total += (probs[c] / probs_sum) * coords
            return coords_total

        GT_TO_ORDER = [2, 5, 3, 8, 6, 7, 0, 9, 1, 4]
        with torch.no_grad():
            best_score, (target, soft_preds) = self._eval_loop(val_loader=self.val_loader, epoch=100000,
                                                               mode=ModelMode.EVAL,
                                                               return_soft_predict=True)
        print(f"best score: {best_score}")
        soft_preds = soft_preds.numpy()
        average_images = self.plot_cluster_average_images(self.val_loader, soft_preds)

        # render point cloud in GT order ---------------------------------------------
        hues = torch.linspace(0.0, 1.0, self.model.arch_dict["output_k_B"] + 1)[0:-1]  # ignore last one
        best_colours = [list((np.array(hsv_to_rgb(hue, 0.8, 0.8)) * 255.).astype(
            np.uint8)) for hue in hues]

        all_colours = [best_colours]

        for colour_i, colours in enumerate(all_colours):
            scale = 50  # [-1, 1] -> [-scale, scale]
            border = 24  # averages are in the borders
            point_half_side = 1  # size 2 * pixel_half_side + 1

            half_border = int(border * 0.5)

            image = np.ones((2 * (scale + border), 2 * (scale + border), 3),
                            dtype=np.uint8) * 255

            for i in range(len(soft_preds)):
                # in range [-1, 1] -> [0, 2 * scale] -> [border, 2 * scale + border]
                coord = get_coord(soft_preds[i, :], num_classes=self.model.arch_dict["output_k_B"])
                coord = (coord * 0.75 * scale + scale).astype(np.int32)
                coord += border
                pt_start = coord - point_half_side
                pt_end = coord + point_half_side

                render_c = GT_TO_ORDER[target[i]]
                colour = (np.array(colours[render_c])).astype(np.uint8)
                image[pt_start[0]:pt_end[0], pt_start[1]:pt_end[1], :] = np.reshape(
                    colour, (1, 1, 3))
            # add average images
            for i in range(10):
                pred = np.zeros(10)
                pred[i] = 1
                coord = get_coord(pred, 10)
                coord = (coord * 1.2 * scale + scale).astype(np.int32)
                coord += border
                pt_start = coord - half_border
                pt_end = coord + half_border
                image[pt_start[0]:pt_end[0], pt_start[1]:pt_end[1], :] = average_images[GT_TO_ORDER[i]].unsqueeze(
                    2).repeat([1, 1, 3]) * 255.0

            # save to out_dir ---------------------------
            img = Image.fromarray(image)
            img.save(self.save_dir / f"best_tsne_{colour_i}_temporature_{temporature}.png")

        self.model.torchnet.head_B.heads = headBheads

    @staticmethod
    def plot_cluster_average_images(val_loader, soft_pred):
        # assert val_loader.dataset_name == "mnist", \
        #     f"save tsne plot is only implemented for MNIST dataset, given {val_loader.dataset_name}."
        from deepclustering.augment.tensor_augment import Resize
        import warnings
        resize_call = Resize((24, 24), interpolation='bilinear')

        average_images = [torch.zeros(24, 24) for _ in range(10)]

        counter = 0
        for image_labels in tqdm_(val_loader):
            images, gt, *_ = list(zip(*image_labels))
            # only take the tf3 image and gts, put them to self.device
            images, gt = images[0].cuda(), gt[0].cuda()
            for i, img in enumerate(images):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    img = resize_call(img.unsqueeze(0))
                average_images[soft_pred[counter + i].argmax()] += img.squeeze().cpu() * soft_pred[counter + i].max()

            counter += len(images)
        assert counter == val_loader.dataset.__len__()
        average_images = [average_image / (counter / 10) for average_image in average_images]
        return average_images

    # 10 point projection ends

    # for tsne projection
    def draw_tsne(self, num_samples=1000):
        self.model.eval()
        images, features, targets = self.feature_exactor(conv_name="trunk", val_loader=self.val_loader)
        idx = torch.randperm(targets.size(0))[:num_samples]
        self.writer.add_embedding(mat=features[idx], metadata=targets[idx], global_step=10000)

    # feature extraction
    def feature_exactor(self, conv_name: str = "trunk", val_loader: DataLoader = None) -> Tuple[Tensor, Tensor, Tensor]:
        assert isinstance(val_loader, DataLoader)
        _images = []
        _features = []
        _targets = []
        _preds = []

        def hook(module, input, output):
            _features.append(output.cpu().detach())

        exec(f"handler=self.model.torchnet.{conv_name}.register_forward_hook(hook)")
        for batch, image_labels in enumerate(val_loader):
            img, gt, *_ = list(zip(*image_labels))
            # only take the tf3 image and gts, put them to self.device
            img, gt = img[0].to(self.device), gt[0].to(self.device)
            # if use sobel filter
            if self.use_sobel:
                img = self.sobel(img)
            # using default head_B for inference, _pred should be a list of simplex by default.
            _pred = self.model.torchnet(img, head="B")[0]
            _images.append(img.cpu())
            _targets.append(gt.cpu())
            _preds.append(_pred.max(1)[1].cpu())
        features = torch.cat(_features, 0)
        targets = torch.cat(_targets, 0)
        images = torch.cat(_images, 0)
        preds = torch.cat(_preds, 0)
        remaped_pred, _ = hungarian_match(
            flat_preds=preds,
            flat_targets=targets,
            preds_k=self.model.arch_dict["output_k_B"],
            targets_k=self.model.arch_dict["output_k_B"]
        )
        acc = flat_acc(remaped_pred, targets)
        assert features.shape[0] == targets.shape[0]
        exec("handler.remove()")
        print(f"Feature exaction ends with acc: {acc:.4f}")
        return images, features, targets

    def linear_retraining(self, conv_name: str, lr=1e-3):
        """
        Calling point to execute retraining
        :param conv_name:
        :return:
        """
        print(f"conv_name: {conv_name}, feature extracting..")

        def _linear_train_loop(train_loader, epoch):
            train_loader_ = tqdm_(train_loader)
            for batch_num, (feature, gt) in enumerate(train_loader_):
                feature, gt = feature.to(self.device), gt.to(self.device)
                pred = linearnet(feature)
                loss = self.criterion(pred, gt)
                linearOptim.zero_grad()
                loss.backward()
                linearOptim.step()
                linear_meters["train_loss"].add(loss.item())
                linear_meters["train_acc"].add(pred.max(1)[1], gt)
                report_dict = {
                    "tra_acc": linear_meters["train_acc"].summary()["acc"],
                    "loss": linear_meters["train_loss"].summary()["mean"],
                }
                train_loader_.set_postfix(report_dict)

            print(f"  Training epoch {epoch}: {nice_dict(report_dict)} ")

        def _linear_eval_loop(val_loader, epoch) -> Tensor:
            val_loader_ = tqdm_(val_loader)
            for batch_num, (feature, gt) in enumerate(val_loader_):
                feature, gt = feature.to(self.device), gt.to(self.device)
                pred = linearnet(feature)
                linear_meters["val_acc"].add(pred.max(1)[1], gt)
                report_dict = {"val_acc": linear_meters["val_acc"].summary()["acc"]}
                val_loader_.set_postfix(report_dict)
            print(f"Validating epoch {epoch}: {nice_dict(report_dict)} ")
            return linear_meters["val_acc"].summary()["acc"]

        # building training and validation set based on extracted features
        train_loader = dcp(self.val_loader)
        train_loader.dataset.datasets = (train_loader.dataset.datasets[0].datasets[0],)
        val_loader = dcp(self.val_loader)
        val_loader.dataset.datasets = (val_loader.dataset.datasets[0].datasets[1],)
        _, train_features, train_targets = self.feature_exactor(conv_name, train_loader)
        print(f"training_feature_shape: {train_features.shape}")
        train_features = train_features.view(train_features.size(0), -1)
        _, val_features, val_targets = self.feature_exactor(conv_name, val_loader)
        val_features = val_features.view(val_features.size(0), -1)
        print(f"val_feature_shape: {val_features.shape}")

        train_dataset = TensorDataset(train_features, train_targets)
        val_dataset = TensorDataset(val_features, val_targets)
        Train_DataLoader = DataLoader(train_dataset, batch_size=100, shuffle=True)
        Val_DataLoader = DataLoader(val_dataset, batch_size=100, shuffle=False)

        # network and optimization
        linearnet = LinearNet(num_features=train_features.size(1), num_classes=self.model.arch_dict["output_k_B"])
        linearOptim = torch.optim.Adam(linearnet.parameters(), lr=lr)
        linearnet.to(self.device)

        # meters
        meter_config = {
            "train_loss": AverageValueMeter(),
            "train_acc": ConfusionMatrix(self.model.arch_dict["output_k_B"]),
            "val_acc": ConfusionMatrix(self.model.arch_dict["output_k_B"])
        }
        linear_meters = MeterInterface(meter_config)
        drawer = DrawCSV2(save_dir=self.save_dir, save_name=f"retraining_from_{conv_name}.png",
                          columns_to_draw=["train_loss_mean",
                                           "train_acc_acc",
                                           "val_acc_acc"])
        for epoch in range(self.max_epoch):
            _linear_train_loop(Train_DataLoader, epoch)
            _ = _linear_eval_loop(Val_DataLoader, epoch)
            linear_meters.step()
            linear_meters.summary().to_csv(self.save_dir / f"retraining_from_{conv_name}.csv")
            drawer.draw(linear_meters.summary())

    def supervised_training(self, use_pretrain=True, lr=1e-3, data_aug=False):
        # load the best checkpoint
        self.load_checkpoint(
            torch.load(str(Path(self.checkpoint) / self.checkpoint_identifier), map_location=torch.device("cpu")))
        self.model.to(self.device)

        from torchvision import transforms
        transform_train = transforms.Compose([
            pil_augment.CenterCrop(size=(20, 20)),
            pil_augment.Resize(size=(32, 32), interpolation=PIL.Image.NEAREST),
            transforms.RandomCrop(32, padding=4),
            transforms.RandomHorizontalFlip(),
            pil_augment.Img2Tensor()
        ])
        transform_val = transforms.Compose([
            pil_augment.CenterCrop(size=(20, 20)),
            pil_augment.Resize(size=(32, 32), interpolation=PIL.Image.NEAREST),
            pil_augment.Img2Tensor()
        ])

        self.kl = KL_div(reduce=True)

        def _sup_train_loop(train_loader, epoch):
            self.model.train()
            train_loader_ = tqdm_(train_loader)
            for batch_num, (image_gt) in enumerate(train_loader_):
                image, gt = zip(*image_gt)
                image = image[0].to(self.device)
                gt = gt[0].to(self.device)

                if self.use_sobel:
                    image = self.sobel(image)

                pred = self.model.torchnet(image)[0]
                loss = self.kl(pred, class2one_hot(gt, 10).float())
                self.model.zero_grad()
                loss.backward()
                self.model.step()
                linear_meters["train_loss"].add(loss.item())
                linear_meters["train_acc"].add(pred.max(1)[1], gt)
                report_dict = {
                    "tra_acc": linear_meters["train_acc"].summary()["acc"],
                    "loss": linear_meters["train_loss"].summary()["mean"],
                }
                train_loader_.set_postfix(report_dict)

            print(f"  Training epoch {epoch}: {nice_dict(report_dict)} ")

        def _sup_eval_loop(val_loader, epoch) -> Tensor:
            self.model.eval()
            val_loader_ = tqdm_(val_loader)
            for batch_num, (image_gt) in enumerate(val_loader_):
                image, gt = zip(*image_gt)
                image = image[0].to(self.device)
                gt = gt[0].to(self.device)

                if self.use_sobel:
                    image = self.sobel(image)

                pred = self.model.torchnet(image)[0]
                linear_meters["val_acc"].add(pred.max(1)[1], gt)
                report_dict = {"val_acc": linear_meters["val_acc"].summary()["acc"]}
                val_loader_.set_postfix(report_dict)
            print(f"Validating epoch {epoch}: {nice_dict(report_dict)} ")
            return linear_meters["val_acc"].summary()["acc"]

            # building training and validation set based on extracted features

        train_loader = dcp(self.val_loader)
        train_loader.dataset.datasets = (train_loader.dataset.datasets[0].datasets[0],)
        val_loader = dcp(self.val_loader)
        val_loader.dataset.datasets = (val_loader.dataset.datasets[0].datasets[1],)

        if data_aug:
            train_loader.dataset.datasets[0].transform = transform_train
            val_loader.dataset.datasets[0].transform = transform_val

        # network and optimization
        if not use_pretrain:
            self.model.torchnet.apply(weights_init)
        else:
            self.model.torchnet.head_B.apply(weights_init)
            # wipe out the initialization
        self.model.optimizer = torch.optim.Adam(self.model.torchnet.parameters(), lr=lr)
        self.model.scheduler = torch.optim.lr_scheduler.StepLR(self.model.optimizer, step_size=50, gamma=0.2)

        # meters
        meter_config = {
            "train_loss": AverageValueMeter(),
            "train_acc": ConfusionMatrix(self.model.arch_dict["output_k_B"]),
            "val_acc": ConfusionMatrix(self.model.arch_dict["output_k_B"])
        }
        linear_meters = MeterInterface(meter_config)
        drawer = DrawCSV2(save_dir=self.save_dir,
                          save_name=f"supervised_from_checkpoint_{use_pretrain}_data_aug_{data_aug}.png",
                          columns_to_draw=["train_loss_mean",
                                           "train_acc_acc",
                                           "val_acc_acc"])
        for epoch in range(self.max_epoch):
            _sup_train_loop(train_loader, epoch)
            with torch.no_grad():
                _ = _sup_eval_loop(val_loader, epoch)
            self.model.step()
            linear_meters.step()
            linear_meters.summary().to_csv(
                self.save_dir / f"supervised_from_checkpoint_{use_pretrain}_data_aug_{data_aug}.csv")
            drawer.draw(linear_meters.summary())

    def draw_IMSAT_table(self, num_samples=20):
        # no shuffle
        from .utils import Image_Pool
        from torchvision.utils import make_grid

        assert isinstance(self.val_loader.sampler, torch.utils.data.SequentialSampler)

        with torch.no_grad():
            best_score, (target, soft_preds) = self._eval_loop(val_loader=self.val_loader, epoch=100000,
                                                               mode=ModelMode.EVAL,
                                                               return_soft_predict=True)
        images = []
        # make cifar10 image to be colorful.
        val_loader = dcp(self.val_loader)
        if val_loader.dataset_name in ("cifar", "svhn"):
            val_loader.dataset.datasets[0].datasets[0].transform.transforms[2] = pil_augment.Img2Tensor(
                include_rgb=True, include_grey=False)
            val_loader.dataset.datasets[0].datasets[1].transform.transforms[2] = pil_augment.Img2Tensor(
                include_rgb=True, include_grey=False)

        for image_gt in val_loader:
            img, gt, *_ = list(zip(*image_gt))
            img, gt = img[0], gt[0]
            images.append(img)

        images = torch.cat(images, 0)
        image_pool = Image_Pool(num_samples, 10)
        image_pool.add(images, torch.Tensor(soft_preds.argmax(dim=1).float()))
        image_dict = image_pool.image_pool()
        first_image_size = make_grid(image_dict[0], nrow=num_samples).shape
        whole_image = torch.ones(first_image_size[0], first_image_size[1] * 10, first_image_size[2])
        for i in range(10):
            whole_image[:, first_image_size[1] * i:first_image_size[1] * (i + 1), :] = make_grid(image_dict[i],
                                                                                                 nrow=num_samples)
        imsat_images = Image.fromarray((whole_image.numpy().transpose(1, 2, 0) * 255.0).astype(np.uint8))
        imsat_images.save(f"{self.save_dir}/imsat_image.png")
