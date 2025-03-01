import logging
import time

import pytest
import concurrent.futures

from ocs_ci.framework.pytest_customization.marks import (
    tier1,
    skipif_lvm_not_installed,
    acceptance,
)
from ocs_ci.framework.testlib import skipif_ocs_version, ManageTest
from ocs_ci.ocs import constants
from ocs_ci.utility.utils import get_ocp_version
from ocs_ci.ocs.cluster import LVM
from ocs_ci.ocs.resources.pod import cal_md5sum
from ocs_ci.ocs.exceptions import Md5CheckFailed

logger = logging.getLogger(__name__)


@pytest.mark.parametrize(
    argnames=["volume_mode", "volume_binding_mode"],
    argvalues=[
        pytest.param(
            *[constants.VOLUME_MODE_FILESYSTEM, constants.WFFC_VOLUMEBINDINGMODE],
            marks=pytest.mark.polarion_id("OCS-3975"),
        ),
        pytest.param(
            *[constants.VOLUME_MODE_BLOCK, constants.WFFC_VOLUMEBINDINGMODE],
            marks=pytest.mark.polarion_id("OCS-3975"),
        ),
        pytest.param(
            *[constants.VOLUME_MODE_FILESYSTEM, constants.IMMEDIATE_VOLUMEBINDINGMODE],
            marks=pytest.mark.polarion_id("OCS-3975"),
        ),
        pytest.param(
            *[constants.VOLUME_MODE_BLOCK, constants.IMMEDIATE_VOLUMEBINDINGMODE],
            marks=pytest.mark.polarion_id("OCS-3975"),
        ),
    ],
)
class TestLvmMultiClone(ManageTest):
    """
    Test multi clone and restore for LVM

    """

    ocp_version = get_ocp_version()
    pvc_size = 100
    access_mode = constants.ACCESS_MODE_RWO
    pvc_num = 5

    @pytest.fixture()
    def namespace(self, project_factory_class):
        self.proj_obj = project_factory_class()
        self.proj = self.proj_obj.namespace

    @pytest.fixture()
    def storageclass(self, lvm_storageclass_factory_class, volume_binding_mode):
        self.sc_obj = lvm_storageclass_factory_class(volume_binding_mode)

    @pytest.fixture()
    def pvc(self, pvc_factory_class, volume_mode, volume_binding_mode):
        self.status = constants.STATUS_PENDING
        if volume_binding_mode == constants.IMMEDIATE_VOLUMEBINDINGMODE:
            self.status = constants.STATUS_BOUND
        futures = []
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.pvc_num)
        for exec_num in range(0, self.pvc_num):
            futures.append(
                executor.submit(
                    pvc_factory_class,
                    project=self.proj_obj,
                    interface=None,
                    storageclass=self.sc_obj,
                    size=self.pvc_size,
                    status=self.status,
                    access_mode=self.access_mode,
                    volume_mode=volume_mode,
                )
            )
        self.pvc_objs = []
        for future in concurrent.futures.as_completed(futures):
            self.pvc_objs.append(future.result())

    @pytest.fixture()
    def pod(self, pod_factory_class, volume_mode):
        self.block = False
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.pvc_num)
        if volume_mode == constants.VOLUME_MODE_BLOCK:
            self.block = True
        futures_pods = []
        self.pods_objs = []
        for pvc in self.pvc_objs:
            futures_pods.append(
                executor.submit(pod_factory_class, pvc=pvc, raw_block_pv=self.block)
            )
        for future_pod in concurrent.futures.as_completed(futures_pods):
            self.pods_objs.append(future_pod.result())

    @pytest.fixture()
    def run_io(self, volume_mode):
        self.fs = "fs"
        self.block = False
        if volume_mode == constants.VOLUME_MODE_BLOCK:
            self.fs = "block"
            self.block = True
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.pvc_num)
        futures_fio = []
        for pod in self.pods_objs:
            futures_fio.append(
                executor.submit(
                    pod.run_io,
                    self.fs,
                    size="5g",
                    rate="1500m",
                    runtime=0,
                    invalidate=0,
                    buffer_compress_percentage=60,
                    buffer_pattern="0xdeadface",
                    bs="1024K",
                    jobs=1,
                    readwrite="readwrite",
                )
            )
        for _ in concurrent.futures.as_completed(futures_fio):
            logger.info("Some pod submitted FIO")
        concurrent.futures.wait(futures_fio)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.pvc_num)
        futures_results = []
        for pod in self.pods_objs:
            futures_results.append(executor.submit(pod.get_fio_results()))
        for _ in concurrent.futures.as_completed(futures_results):
            logger.info("Just waiting for fio jobs results")
        concurrent.futures.wait(futures_results)
        if not self.block:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.pvc_num)
            futures_md5 = []
            self.origin_pods_md5 = []
            for pod in self.pods_objs:
                futures_md5.append(
                    executor.submit(
                        cal_md5sum,
                        pod_obj=pod,
                        file_name="fio-rand-readwrite",
                        block=self.block,
                    )
                )
            for future_md5 in concurrent.futures.as_completed(futures_md5):
                self.origin_pods_md5.append(future_md5.result())

    @pytest.fixture()
    def create_clone(self, pvc_clone_factory, volume_mode):
        logger.info("Creating snapshot from pvc objects")
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.pvc_num)
        futures_clone = []
        self.clone_objs = []
        for pvc in self.pvc_objs:
            futures_clone.append(
                executor.submit(
                    pvc_clone_factory,
                    pvc_obj=pvc,
                    status=self.status,
                    volume_mode=volume_mode,
                )
            )
        for future_clone in concurrent.futures.as_completed(futures_clone):
            self.clone_objs.append(future_clone.result())
        concurrent.futures.wait(futures_clone)

    @tier1
    @acceptance
    @skipif_lvm_not_installed
    @skipif_ocs_version("<4.10")
    def test_create_multi_clone_from_pvc(
        self,
        namespace,
        storageclass,
        pvc,
        pod,
        run_io,
        create_clone,
        pod_factory,
    ):
        """
        test create delete multi snapshot
        .* Create 5 PVC
        .* Create 5 POD
        .* Run IO
        .* Create 5 clones
        .* Create 5 pvc from clone
        .* Attach 5 pod
        .* Run IO

        """
        lvm = LVM()
        logger.info(f"LVMCluster version is {lvm.get_lvm_version()}")
        logger.info(
            f"Lvm thin-pool overprovisionRation is {lvm.get_lvm_thin_pool_config_overprovision_ratio()}"
        )
        logger.info(
            f"Lvm thin-pool sizePercent is {lvm.get_lvm_thin_pool_config_size_percent()}"
        )

        logger.info("Attaching pods to pvcs restores")
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.pvc_num)
        futures_restored_pods = []
        restored_pods_objs = []

        for pvc in self.clone_objs:
            futures_restored_pods.append(
                executor.submit(pod_factory, pvc=pvc, raw_block_pv=self.block)
            )
        for future_restored_pod in concurrent.futures.as_completed(
            futures_restored_pods
        ):
            restored_pods_objs.append(future_restored_pod.result())
        concurrent.futures.wait(futures_restored_pods)
        time.sleep(10)

        if not self.block:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.pvc_num)
            futures_restored_pods_md5 = []
            restored_pods_md5 = []
            for restored_pod in restored_pods_objs:
                futures_restored_pods_md5.append(
                    executor.submit(
                        cal_md5sum,
                        pod_obj=restored_pod,
                        file_name="fio-rand-readwrite",
                        block=self.block,
                    )
                )
            for future_restored_pod_md5 in concurrent.futures.as_completed(
                futures_restored_pods_md5
            ):
                restored_pods_md5.append(future_restored_pod_md5.result())
            for pod_num in range(0, self.pvc_num):
                if self.origin_pods_md5[pod_num] != restored_pods_md5[pod_num]:
                    raise Md5CheckFailed(
                        f"origin pod {self.pods_objs[pod_num]} md5 value {self.origin_pods_md5[pod_num]} "
                        f"is not the same as restored pod {restored_pods_objs[pod_num]} md5 "
                        f"value {restored_pods_md5[pod_num]}"
                    )
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.pvc_num)
        futures_restored_pods_fio = []

        for restored_pod in restored_pods_objs:
            futures_restored_pods_fio.append(
                executor.submit(
                    restored_pod.run_io,
                    self.fs,
                    size="1g",
                    rate="1500m",
                    runtime=0,
                    invalidate=0,
                    buffer_compress_percentage=60,
                    buffer_pattern="0xdeadface",
                    bs="1024K",
                    jobs=1,
                    readwrite="readwrite",
                )
            )
        for _ in concurrent.futures.as_completed(futures_restored_pods_fio):
            logger.info("Waiting for all fio pods submission")
        concurrent.futures.wait(futures_restored_pods_fio)

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.pvc_num)
        futures_restored_pods_fio_results = []
        for restored_pod in restored_pods_objs:
            futures_restored_pods_fio_results.append(
                executor.submit(restored_pod.get_fio_results())
            )
        for _ in concurrent.futures.as_completed(futures_restored_pods_fio_results):
            logger.info("Finished waiting for some pod")
        concurrent.futures.wait(futures_restored_pods_fio_results)
        if not self.block:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.pvc_num)
            futures_restored_pods_md5_after_fio = []
            restored_pods_md5_after_fio = []
            for restored_pod in restored_pods_objs:
                futures_restored_pods_md5_after_fio.append(
                    executor.submit(
                        cal_md5sum,
                        pod_obj=restored_pod,
                        file_name="fio-rand-readwrite",
                        block=self.block,
                    )
                )
            for future_restored_pods_md5_after_fio in concurrent.futures.as_completed(
                futures_restored_pods_md5_after_fio
            ):
                restored_pods_md5_after_fio.append(
                    future_restored_pods_md5_after_fio.result()
                )

            for pod_num in range(0, self.pvc_num):
                if (
                    restored_pods_md5_after_fio[pod_num]
                    == self.origin_pods_md5[pod_num]
                ):
                    raise Md5CheckFailed(
                        f"origin pod {self.pods_objs[pod_num].name} md5 value {self.origin_pods_md5[pod_num]} "
                        f"is not suppose to be the same as restored pod {restored_pods_objs[pod_num].name} md5 "
                        f"value {restored_pods_md5_after_fio[pod_num]}"
                    )
