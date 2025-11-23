import os
import boto3
import logging
from botocore.exceptions import ClientError
import json
from datetime import datetime, timedelta, UTC
from tempfile import TemporaryDirectory
import ipaddress
import zipfile

# Setup logging
logging.basicConfig(
    level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s"
)
LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

EC2 = boto3.client("ec2")


def lambda_handler(event, context):
    try:
        response = main(event, context)
        return {"statusCode": 200, "body": json.dumps(response)}
    except AssertionError as e:
        LOGGER.error(str(e), exc_info=True, stack_info=True)
        return {"statusCode": 400, "body": str(e)}
    except PermissionError as e:
        LOGGER.error(str(e), exc_info=True, stack_info=True)
        return {"statusCode": 403, "body": str(e)}


def main(event, context):
    LOGGER.info(json.dumps(event))

    # if event comes from api gateway, log details then extract body
    if "body" in event:
        LOGGER.info(f"API Gateway event received")
        action = event["path"].lstrip("/")
        checkApiSourceIp(event, log_message_prefix=f"{action} request from ")
    else:
        # event comes direct (i.e. from eventbridge scheduler)
        action = event["scheduled_event"]

    actions = {
        "start_valheim_server": lambda: [
            createBackup(),
            startInstance(os.environ["ValheimEC2InstanceId"]),
        ][1],
        "stop_valheim_server": lambda: stopInstance(
            os.environ["ValheimEC2InstanceId"], force=False
        ),
        "force_stop_valheim_server": lambda: stopInstance(
            os.environ["ValheimEC2InstanceId"], force=True
        ),
    }

    # Now execute request
    if action in actions:
        return actions[action]()
    else:
        raise AssertionError(
            f"Invalid action: {action}. "
            f"Action must be one of: {list(actions.keys())}"
        )


def checkApiSourceIp(event, log_message_prefix=None):
    """Checks if the source IP of the API request is in the allowed list."""
    prefixListId = os.environ.get("AllowedIpPrefixListId")
    response = EC2.get_managed_prefix_list_entries(PrefixListId=prefixListId)
    source_ip = ipaddress.ip_address(event["requestContext"]["identity"]["sourceIp"])
    for entry in response["Entries"]:
        if source_ip in ipaddress.ip_network(entry["Cidr"]):
            LOGGER.info(f"{log_message_prefix}{entry['Description']}")
            return
    raise PermissionError(
        f"{log_message_prefix}Source IP {source_ip} not in allowed list."
    )


def get_instance_state(InstanceId):
    try:
        response = EC2.describe_instances(InstanceIds=[InstanceId])
        state = response["Reservations"][0]["Instances"][0]["State"]["Name"]
        return state
    except ClientError as e:
        LOGGER.error(f"Error getting instance state: {e.response['Error']['Message']}")
        raise


def startInstance(InstanceId):
    LOGGER.info(f"Checking instance status: {InstanceId}")
    instance_state = get_instance_state(InstanceId)
    if instance_state != "running":
        LOGGER.info(f"Starting EC2 instance: {InstanceId}")
        EC2.start_instances(InstanceIds=[InstanceId])
        LOGGER.info(f"Started EC2 instance: {InstanceId}")
        return {"Message": f"EC2 instance {InstanceId} is starting."}
    else:
        LOGGER.info(f"Instance already running: {InstanceId}")
        return {"Message": f"EC2 instance {InstanceId} is already running."}


def createBackup():
    new_name = f"valheim_config_backup_{datetime.now():%Y-%m-%d_%H-%M-%S}.zip"
    LOGGER.info(f"Creating backup on S3 {new_name}")
    s3 = boto3.resource("s3")
    bucket = s3.Bucket(os.environ["ValheimS3BucketName"])
    with TemporaryDirectory() as tmpdir:
        for obj in bucket.objects.filter(Prefix="config/"):
            if obj.key.endswith("/"):
                continue  # skip directories
            target = os.path.join(tmpdir, os.path.relpath(obj.key, "config/"))
            LOGGER.info(f"Downloading {obj.key} to {target}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            bucket.download_file(obj.key, target)
        archive_path = os.path.join(tmpdir, new_name)
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmpdir, arcname="config")
        bucket.upload_file(archive_path, new_name)
    # also delete old backups
    backups = sorted(
        [
            obj
            for obj in bucket.objects.filter(Prefix="valheim_config_backup_")
        ],
        key=lambda x: x.key,
        reverse=True,
    )
    if len(backups) < 5:
        for old_backup in backups[5:]:
            LOGGER.info(f"Deleting old backup {old_backup.key}")
            old_backup.delete()


def instance_in_use(InstanceId, lookback_hours=1, usage_threshold=10.0, period=300):
    cw = boto3.client("cloudwatch")
    try:
        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(hours=lookback_hours)

        response = cw.get_metric_statistics(
            Namespace="AWS/EC2",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "InstanceId", "Value": InstanceId}],
            StartTime=start_time,
            EndTime=end_time,
            Period=period,
            Statistics=["Average"],
        )

        expected_datapoints = lookback_hours * 3600 // period
        actual_datapoints = len(response["Datapoints"])
        if (
            actual_datapoints < expected_datapoints * 0.9
        ):  # a few of the latest datapoints may not be available
            LOGGER.info(f"Instance {InstanceId} was recently turned on - not stopping.")
            return True

        for datapoint in response["Datapoints"]:
            if datapoint["Average"] > usage_threshold:
                LOGGER.info(
                    f"Instance {InstanceId} has recent CPU activity - not stopping."
                )
                LOGGER.info(f"Datapoint: {datapoint}")
                return True
        # If all checks pass, then instance is not in use
        return False

    except ClientError as e:
        LOGGER.error(
            f"Error checking CPU metrics: {e.response['Error']['Message']}",
            exc_info=True,
        )
        return True


def stopInstance(InstanceId, force=False):
    LOGGER.info(f"Checking instance status: {InstanceId}")
    instance_state = get_instance_state(InstanceId)
    if instance_state == "running":
        if force:
            LOGGER.info(f"Stopping EC2 instance: {InstanceId}")
            EC2.stop_instances(InstanceIds=[InstanceId])
            LOGGER.info(f"Stopped EC2 instance: {InstanceId}")
            return {"Message": f"EC2 instance {InstanceId} is stopping."}
        else:
            # Check CPU usage before stopping
            if instance_in_use(InstanceId):
                return {
                    "Message": f"EC2 instance {InstanceId} has recent CPU activity - not stopping."
                }
            else:
                LOGGER.info(f"Stopping inactive EC2 instance: {InstanceId}")
                EC2.stop_instances(InstanceIds=[InstanceId])
                LOGGER.info(f"Stopped EC2 instance: {InstanceId}")
                return {"Message": f"Inactive EC2 instance {InstanceId} is stopping."}
    else:
        LOGGER.info(f"Instance already stopped: {InstanceId}")
        return {"Message": f"EC2 instance {InstanceId} is not running."}
