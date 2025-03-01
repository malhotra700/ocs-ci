import logging
import pytest

from ocs_ci.ocs import constants
from ocs_ci.framework.testlib import (
    skipif_ocs_version,
    ManageTest,
    tier1,
    polarion_id,
)
from ocs_ci.ocs.exceptions import (
    CommandFailed,
    TimeoutExpiredError,
    UnexpectedBehaviour,
)
from ocs_ci.ocs.resources.pod import get_file_path, check_file_existence
from ocs_ci.helpers.helpers import fetch_used_size
from ocs_ci.utility.utils import TimeoutSampler

log = logging.getLogger(__name__)


@skipif_ocs_version("<4.10")
class TestRbdSpaceReclaim(ManageTest):
    """
    Tests to verify RBD space reclamation
    """

    @pytest.fixture(autouse=True)
    def setup(self, project_factory, storageclass_factory, create_pvcs_and_pods):
        """
        Create PVCs and pods
        """
        self.pool_replica = 3
        pvc_size_gi = 25
        self.sc_obj = storageclass_factory(
            interface=constants.CEPHBLOCKPOOL,
            replica=self.pool_replica,
            new_rbd_pool=False,
        )
        self.pvc, self.pod = create_pvcs_and_pods(
            pvc_size=pvc_size_gi,
            access_modes_rbd=[constants.ACCESS_MODE_RWO],
            num_of_rbd_pvc=1,
            num_of_cephfs_pvc=0,
            sc_rbd=self.sc_obj,
        )

    @polarion_id("OCS-2759")
    @tier1
    def test_rbd_space_reclaim_cronjob(self):
        """
        Test to verify RBD space reclamation
        Steps:
        1. Create and attach RBD PVC of size 25 GiB to an app pod.
        2. Get the used size of the RBD pool
        3. Create four files of size 4GiB
        4. Verify the increased used size of the RBD pool
        5. Delete three file
        6. Create ReclaimSpaceJob
        7. Verify the decreased used size of the RBD pool
        8. Verify the presence of other files in the folder
        """
        pvc_obj = self.pvc[0]
        pod_obj = self.pod[0]

        fio_filename1 = "fio_file1"
        fio_filename2 = "fio_file2"
        fio_filename3 = "fio_file3"
        fio_filename4 = "fio_file4"

        schedule = ["hourly", "midnight", "weekly"]
        # Fetch the used size of pool
        cbp_name = self.sc_obj.get().get("parameters").get("pool")
        log.info(f"Cephblock pool name {cbp_name}")
        used_size_before_io = fetch_used_size(cbp_name)
        log.info(f"Used size before IO is {used_size_before_io}")

        # Create four 4 GB file
        for filename in [fio_filename1, fio_filename2, fio_filename3, fio_filename4]:
            pod_obj.run_io(
                storage_type="fs",
                size="4G",
                runtime=100,
                fio_filename=filename,
                end_fsync=1,
            )
            pod_obj.get_fio_results()

        # Verify used size after IO
        exp_used_size_after_io = used_size_before_io + (16 * self.pool_replica)
        used_size_after_io = fetch_used_size(cbp_name, exp_used_size_after_io)
        log.info(f"Used size after IO is {used_size_after_io}")

        # Delete the file and validate the reclaimspace cronjob
        for filename in [fio_filename1, fio_filename2, fio_filename3]:
            file_path = get_file_path(pod_obj, filename)
            pod_obj.exec_cmd_on_pod(
                command=f"rm -f {file_path}", out_yaml_format=False, timeout=100
            )

            # Verify file is deleted
            try:
                check_file_existence(pod_obj=pod_obj, file_path=file_path)
            except CommandFailed as cmdfail:
                if "No such file or directory" not in str(cmdfail):
                    raise
                log.info(f"Verified: File {file_path} deleted.")

        # Create ReclaimSpaceCronJob
        for type in schedule:
            reclaim_space_job = pvc_obj.create_reclaim_space_cronjob(schedule=type)

            # Wait for the Succeeded result of ReclaimSpaceJob
            try:
                for reclaim_space_job_yaml in TimeoutSampler(
                    timeout=120, sleep=5, func=reclaim_space_job.get
                ):
                    result = reclaim_space_job_yaml["spec"]["schedule"]
                    if result == "@" + type:
                        log.info(f"ReclaimSpaceJob {reclaim_space_job.name} succeeded")
                        break
                    else:
                        log.info(
                            f"Waiting for the Succeeded result of the ReclaimSpaceCronJob {reclaim_space_job.name}. "
                            f"Present value of result is {result}"
                        )
            except TimeoutExpiredError:
                raise UnexpectedBehaviour(
                    f"ReclaimSpaceJob {reclaim_space_job.name} is not successful. Yaml output:{reclaim_space_job.get()}"
                )

        # Verify the presence of another file in the directory
        log.info("Verifying the existence of remaining file in the directory")
        file_path = get_file_path(pod_obj, fio_filename4)
        log.info(check_file_existence(pod_obj=pod_obj, file_path=file_path))
        if check_file_existence(pod_obj=pod_obj, file_path=file_path):
            log.info(f"{fio_filename4} is intact")
