__all__ = ["SVHNClusteringDatasetInterface", "svhn_naive_transform", "svhn_strong_transform"]

from functools import reduce
from typing import List, Callable

import PIL
from deepclustering.augment import pil_augment
from torchvision import transforms

from .clustering_helper import ClusterDatasetInterface
from .svhn import SVHN


class SVHNClusteringDatasetInterface(ClusterDatasetInterface):
    ALLOWED_SPLIT = ["train", "test"]

    def __init__(
            self,
            data_root=None,
            split_partitions: List[str] = [],
            batch_size: int = 1,
            shuffle: bool = False,
            num_workers: int = 1,
            pin_memory: bool = True,
    ) -> None:
        super().__init__(
            SVHN,
            data_root,
            split_partitions,
            batch_size,
            shuffle,
            num_workers,
            pin_memory,
        )

    def _creat_concatDataset(
            self,
            image_transform: Callable,
            target_transform: Callable,
            dataset_dict: dict = {},
    ):
        for split in self.split_partitions:
            assert (
                    split in self.ALLOWED_SPLIT
            ), f"Allowed split in SVHN:{self.ALLOWED_SPLIT}, given {split}."

        _datasets = []
        for split in self.split_partitions:
            dataset = self.DataClass(
                self.data_root,
                split=split,
                transform=image_transform,
                target_transform=target_transform,
                download=True,
                **dataset_dict,
            )
            _datasets.append(dataset)
        serial_dataset = reduce(lambda x, y: x + y, _datasets)
        return serial_dataset


# ===================== public transform interface ===========================
svhn_naive_transform = {
    # output size 32*32
    "tf1": transforms.Compose([
        transforms.ToTensor(),
    ]),
    "tf2": transforms.Compose([
        pil_augment.RandomCrop(size=32, padding=2, ),
        transforms.ToTensor(),
    ]
    ),
    "tf3": transforms.Compose([
        transforms.ToTensor(),
    ]),
}
svhn_strong_transform = {
    # output size 32*32
    "tf1": transforms.Compose([
        pil_augment.CenterCrop(size=(28, 28)),
        pil_augment.Resize(size=32, interpolation=PIL.Image.BILINEAR),
        transforms.ToTensor()]),
    "tf2": transforms.Compose([pil_augment.RandomApply(
        transforms=[transforms.RandomRotation(degrees=(-25.0, 25.0), resample=False, expand=False)],
        p=0.5),
        pil_augment.RandomChoice(transforms=[
            pil_augment.RandomCrop(size=(20, 20), padding=None),
            pil_augment.RandomCrop(size=(24, 24), padding=None),
            pil_augment.RandomCrop(size=(28, 28), padding=None)]),
        pil_augment.Resize(size=32, interpolation=PIL.Image.BILINEAR),
        transforms.ColorJitter(
            brightness=[0.6, 1.4],
            contrast=[0.6, 1.4],
            saturation=[0.6, 1.4],
            hue=[-0.125, 0.125]),
        transforms.ToTensor()]),
    "tf3": transforms.Compose([
        pil_augment.CenterCrop(size=(28, 28)),
        pil_augment.Resize(size=32, interpolation=PIL.Image.BILINEAR),
        transforms.ToTensor()]),
}
# ============================================================================================
