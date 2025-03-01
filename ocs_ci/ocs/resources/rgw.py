import base64

from ocs_ci.framework import config
from ocs_ci.ocs.ocp import OCP
from ocs_ci.ocs import constants
from ocs_ci.helpers.helpers import storagecluster_independent_check
from ocs_ci.utility import version


class RGW(object):
    """
    Wrapper class for interaction with a cluster's RGW service
    """

    def __init__(self, namespace=None):
        self.namespace = (
            namespace if namespace else config.ENV_DATA["cluster_namespace"]
        )

        if storagecluster_independent_check():
            sc_name = constants.DEFAULT_EXTERNAL_MODE_STORAGECLASS_RGW
        else:
            sc_name = constants.DEFAULT_STORAGECLASS_RGW

        self.storageclass = OCP(
            kind="storageclass", namespace=namespace, resource_name=sc_name
        )
        self.s3_internal_endpoint = (
            self.storageclass.get().get("parameters").get("endpoint")
        )
        self.region = self.storageclass.get().get("parameters").get("region")
        # Todo: Implement retrieval in cases where CephObjectStoreUser is available
        self.key_id = None
        self.secret_key = None
        self.s3_resource = None

    def get_credentials(self, secret_name=constants.NOOBAA_OBJECTSTOREUSER_SECRET):
        """
        Get Endpoint, Access key and Secret key from OCS secret. Endpoint is
        taken from rgw exposed service. Use rgw_endpoint fixture in test to get
        it exposed.

        Args:
            secret_name (str): Name of secret to be used
                for getting RGW credentials

        Returns:
            tuple: Endpoint, Access key, Secret key

        """
        secret_ocp_obj = OCP(kind=constants.SECRET, namespace=self.namespace)
        route_ocp_obj = OCP(
            kind=constants.ROUTE, namespace=config.ENV_DATA["cluster_namespace"]
        )

        if storagecluster_independent_check():
            if version.get_semantic_ocs_version_from_config() < version.VERSION_4_7:
                endpoint = route_ocp_obj.get(
                    resource_name=constants.RGW_SERVICE_EXTERNAL_MODE
                )
            else:
                endpoint = route_ocp_obj.get(
                    resource_name=constants.RGW_ROUTE_EXTERNAL_MODE
                )
            if secret_name == constants.NOOBAA_OBJECTSTOREUSER_SECRET:
                secret_name = constants.EXTERNAL_MODE_NOOBAA_OBJECTSTOREUSER_SECRET
            elif secret_name == constants.CEPH_OBJECTSTOREUSER_SECRET:
                secret_name = constants.CEPH_EXTERNAL_OBJECTSTOREUSER_SECRET
        else:
            if version.get_semantic_ocs_version_from_config() < version.VERSION_4_7:
                endpoint = route_ocp_obj.get(
                    resource_name=constants.RGW_SERVICE_INTERNAL_MODE
                )
            else:
                endpoint = route_ocp_obj.get(
                    resource_name=constants.RGW_ROUTE_INTERNAL_MODE
                )

        creds_secret_obj = secret_ocp_obj.get(secret_name)
        endpoint = f"http://{endpoint['status']['ingress'][0]['host']}"
        access_key = base64.b64decode(
            creds_secret_obj.get("data").get("AccessKey")
        ).decode("utf-8")
        secret_key = base64.b64decode(
            creds_secret_obj.get("data").get("SecretKey")
        ).decode("utf-8")
        return (endpoint, access_key, secret_key)
