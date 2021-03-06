import json
import time
import asyncio
import logging
import requests

from datetime import datetime
from requests.exceptions import ConnectionError, Timeout

from common.config import Config as config
from common.decorators import retry

logger = logging.getLogger(__name__)

NEGATIVE_STATES = ['STOPPED', 'STOPPING', 'ERROR', 'CRASHED']
POSITIVE_STATES = ['RUNNING', 'PROVISIONING', 'CREATING']

IAM_URL = 'https://iam.api.cloud.yandex.net/iam/v1/tokens'
SNAP_URL = 'https://compute.api.cloud.yandex.net/compute/v1/snapshots/'
COMPUTE_URL = 'https://compute.api.cloud.yandex.net/compute/v1/instances/'
DISK_URL = 'https://compute.api.cloud.yandex.net/compute/v1/disks/'
OPERATION_URL = 'https://operation.api.cloud.yandex.net/operations/'


class Instance:

    '''
    Yandex Cloud Instance Model.

    Attributes:
      :folder_id: str
      :name: str
      :boot_disk: str
      :secondary_disks: list
      :status: str

    Methods:
      get_all_snapshots() -> return list of snapshots id
      get_old_snapshots() -> return list of old snapshots id
      start() -> return operation id as str
      stop() -> return operation id as str
      create_snapshot() -> return operation id as str
      delete_snapshot() -> return operaion id as str 
    '''

    def __init__(self, instance_id):
        self.iam_token = self.get_iam(config.oauth_token)
        self.instance_id = instance_id
        self.lifetime = int(config.lifetime)
        self.headers = {
            'Authorization': f'Bearer {self.iam_token}',
            'content-type': 'application/json'
        }
        self.instance_data = self.get_data()

    @retry((ConnectionError, Timeout))
    def get_iam(self, token):
        r = requests.post(IAM_URL, json={'yandexPassportOauthToken': token})
        data = json.loads(r.text)

        if r.status_code != 200:
            logger.error(f'{r.status_code} Error in get_iam: {data["message"]}')
            quit()

        else:
            iam_token = data.get('iamToken')
            return iam_token

    def call_time(self):
        current_time = datetime.now()
        raw_time = current_time.strftime('%d-%m-%Y-%H-%M-%S')
        return raw_time

    @retry((ConnectionError, Timeout))
    def get_data(self):
        r = requests.get(COMPUTE_URL + self.instance_id, headers=self.headers)
        res = json.loads(r.text)

        if r.status_code == 404:
            logger.warning(f'Instance with ID {self.instance_id} not exist')
        elif r.status_code != 200:
            logger.error(f'{r.status_code} Error in get_data: {res["message"]}')
        else:
            return res

    @property
    def folder_id(self):
        if self.instance_data is None:
            return

        folder_id = self.instance_data.get('folderId')
        return folder_id

    @property
    def name(self):
        if self.instance_data is None:
            return

        name = self.instance_data.get('name')
        return name

    @property
    def boot_disk(self):
        if self.instance_data is None:
            return

        boot_disk = self.instance_data['bootDisk']['diskId']
        return boot_disk

    @property
    def secondary_disks(self):
        if self.instance_data is None:
            return

        _disks = self.instance_data.get('secondaryDisks')
        disks = [x.get('diskId') for x in _disks] if _disks else []
        return disks

    @property
    def status(self):
        if self.instance_data is None:
            return 'NON-EXISTENT'

        status = self.get_data().get('status')
        return status

    def __repr__(self):
        data = {
            "InstanceID": self.instance_id,
            "FolderID": self.folder_id,
            "Name": self.name,
            "BootDisk": self.boot_disk,
            "Status": self.status
        }

        return data

    def __str__(self):
        try:
            data = self.__repr__()
            result = ", ".join([f'{key}: {value}' for key, value in data.items()])
            return result

        except (TypeError, AttributeError):
            logger.info(f'Instance with ID {self.instance_id} not found.')

    @retry((ConnectionError, Timeout))
    def get_all_snapshots(self):
        try:
            r = requests.get(SNAP_URL, headers=self.headers, json={'folderId': self.folder_id})
            res = json.loads(r.text)

            if r.status_code != 200:
                logger.error(f'{r.status_code} Error in get_all_snapshots: {res["message"]}')

            else:
                result = []
                snapshots = res.get('snapshots')

                for snapshot in snapshots:
                    if snapshot['sourceDiskId'] == self.boot_disk:
                        result.append(snapshot)

                return result

        except TypeError:
            logger.info(f'Snapshots for {self.name} not found.')
        except AttributeError:
            logger.warning(f"Can't find snapshots for non-existent instance {self.instance_id}")

    def get_old_snapshots(self):
        result = []
        all_snapshots = self.get_all_snapshots()
    
        if all_snapshots:
            for snapshot in all_snapshots:
                created_at = datetime.strptime(snapshot['createdAt'], '%Y-%m-%dT%H:%M:%Sz')
                today = datetime.utcnow()
                age = int((today - created_at).total_seconds()) // 86400

                if age >= self.lifetime:
                    result.append(snapshot)

            return result

    @retry((ConnectionError, Timeout))
    def operation_status(self, operation_id):
        try:
            r = requests.get(OPERATION_URL + operation_id, headers=self.headers)
            res = json.loads(r.text)

            if r.status_code != 200:
                logger.error(f'{r.status_code} Error in operation_status: {res["message"]}')

            else:
                return res

        except requests.exceptions.ConnectionError:
            logger.warning('Connection error. Please check your network connection')
        except Exception as err:
            logger.error(f'Error in operation_status: {err}')

    async def async_operation_complete(self, operation_id):
        if operation_id:
            timeout = 0
            while True:
                operation = self.operation_status(operation_id)
                await asyncio.sleep(2)
                timeout += 2

                if operation.get('done') is True:
                    msg = f'Operation {operation.get("description")} with ID {operation_id} completed'
                    logger.info(msg)
                    return msg

                elif timeout == 600:
                    msg = f'Operation {operation.get("description")} with {operation_id} running too long.'
                    logger.warning(msg)
                    return msg

    def operation_complete(self, operation_id):
        if operation_id:
            timeout = 0
            while True:
                operation = self.operation_status(operation_id)
                time.sleep(2)
                timeout += 2

                if operation.get('done') is True:
                    msg = f'Operation {operation.get("description")} with ID {operation_id} completed'
                    logger.info(msg)
                    return msg

                elif timeout == 600:
                    msg = f'Operation {operation.get("description")} with {operation_id} running too long.'
                    logger.warning(msg)
                    return msg

    @retry((ConnectionError, Timeout))
    def start(self):
        if self.status not in POSITIVE_STATES:
            r = requests.post(COMPUTE_URL + f'{self.instance_id}:start', headers=self.headers)
            res = json.loads(r.text)

            if r.status_code != 200:
                logger.error(f'{r.status_code} Error in start_vm: {res["message"]}')

            else:
                logger.info(f'Starting instance {self.name} ({self.instance_id})')
                # Return operation ID
                return res.get('id')

        else:
            logger.warning(f'Instance {self.name} has an invalid state for this operation.')


    @retry((ConnectionError, Timeout))
    def restart(self):
        if self.status not in NEGATIVE_STATES:
            r = requests.post(COMPUTE_URL + f'{self.instance_id}:restart', headers=self.headers)
            res = json.loads(r.text)

            if r.status_code != 200:
                logger.error(f'{r.status_code} Error in restart_vm: {res["message"]}')

            else:
                logger.info(f'Restarting instance {self.name} ({self.instance_id})')
                # Return operation ID
                return res.get('id')

        else:
            logger.warning(f'Instance {self.name} has an invalid state for this operation.')


    @retry((ConnectionError, Timeout))
    def stop(self):
        if self.status not in NEGATIVE_STATES:
            r = requests.post(COMPUTE_URL + f'{self.instance_id}:stop', headers=self.headers)
            res = json.loads(r.text)

            if r.status_code != 200:
                logger.error(f'{r.status_code} Error in stop_vm: {res["message"]}')
            else:
                logger.info(f'Stopping instance {self.name} ({self.instance_id})')
                # Return operation ID
                return res.get('id')

        elif self.status == 'STOPPED':
            logger.info(f'Instance {self.name} already stopped.')
        else:
            logger.warning(f'Instance {self.name} has an invalid state for this operation.')

    @retry((ConnectionError, Timeout))
    def create_snapshot(self, disk_id=None):
        data = {
            'folderId': self.folder_id,
            'diskId': self.boot_disk if disk_id is None else disk_id,
            'name': f'{self.name}-{self.call_time()}'
        }
        r = requests.post(SNAP_URL, json=data, headers=self.headers)
        res = json.loads(r.text)

        if r.status_code == 429:
            logger.warning(f'Snapshot NOT CREATED for instance {self.name}. Error: {res["message"]}')
            logger.error(f'QUOTA ERROR: {res["message"]}')

        elif r.status_code != 200:
            logger.error(f'{r.status_code} Error in create_snapshot: {res["message"]}')

        else:
            logger.info(f'Starting create snapshot for boot-disk {self.boot_disk} on {self.name}')
            # Return operation ID
            return res.get('id')

    @retry((ConnectionError, Timeout))
    def delete_snapshot(self, data=None, snapshot_id=None):
        if not data and not snapshot_id:
            logging.error('dict data or snapshot_id required')
            return

        snapshot = data.get('id') if snapshot_id is None else snapshot_id
        snapshot_name = data.get('name') if snapshot_id is None else snapshot_id

        r = requests.delete(SNAP_URL + snapshot, headers=self.headers)
        res = json.loads(r.text)

        if r.status_code != 200:
            logger.error(f'{r.status_code} Error in delete_snapshot: {res.get("message")}')
        else:
            logger.info(f'Starting delete snapshot {snapshot_name}')
            # Return operation ID
            return res.get('id')
