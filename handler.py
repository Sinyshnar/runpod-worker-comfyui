import os
import shutil
import time
import requests
import traceback
import json
import base64
import uuid
import logging
import logging.handlers
import runpod
import io
from PIL import Image
from runpod.serverless.utils.rp_validator import validate
from runpod.serverless.modules.rp_logger import RunPodLogger
from requests.adapters import HTTPAdapter, Retry
from schemas.input import INPUT_SCHEMA


APP_NAME = 'runpod-worker-comfyui'
BASE_URI = 'http://127.0.0.1:3000'
VOLUME_MOUNT_PATH = '/runpod-volume'
LOG_FILE = 'comfyui-worker.log'
TIMEOUT = 600
LOG_LEVEL = 'INFO'
DISK_MIN_FREE_BYTES = 500 * 1024 * 1024  # 500MB in bytes
JPEG_QUALITY = 95  # JPEG quality for output images (1-100, higher = better quality)


# ---------------------------------------------------------------------------- #
#                               Custom Log Handler                             #
# ---------------------------------------------------------------------------- #
class SnapLogHandler(logging.Handler):
    def __init__(self, app_name: str):
        super().__init__()
        self.app_name = app_name
        self.rp_logger = RunPodLogger()
        self.rp_logger.set_level(LOG_LEVEL)
        self.runpod_endpoint_id = os.getenv('RUNPOD_ENDPOINT_ID')
        self.runpod_cpu_count = os.getenv('RUNPOD_CPU_COUNT')
        self.runpod_pod_id = os.getenv('RUNPOD_POD_ID')
        self.runpod_gpu_size = os.getenv('RUNPOD_GPU_SIZE')
        self.runpod_mem_gb = os.getenv('RUNPOD_MEM_GB')
        self.runpod_gpu_count = os.getenv('RUNPOD_GPU_COUNT')
        self.runpod_volume_id = os.getenv('RUNPOD_VOLUME_ID')
        self.runpod_pod_hostname = os.getenv('RUNPOD_POD_HOSTNAME')
        self.runpod_debug_level = os.getenv('RUNPOD_DEBUG_LEVEL')
        self.runpod_dc_id = os.getenv('RUNPOD_DC_ID')
        self.runpod_gpu_name = os.getenv('RUNPOD_GPU_NAME')
        self.log_api_endpoint = os.getenv('LOG_API_ENDPOINT')
        self.log_api_timeout = os.getenv('LOG_API_TIMEOUT', 5)
        self.log_api_timeout = int(self.log_api_timeout)
        self.log_token = os.getenv('LOG_API_TOKEN')

    def emit(self, record):
        runpod_job_id = os.getenv('RUNPOD_JOB_ID')

        try:
            # Handle string formatting and extra arguments
            if hasattr(record, 'msg') and hasattr(record, 'args'):
                if record.args:
                    try:
                        # Try to format the message with args
                        if isinstance(record.args, dict):
                            message = record.msg % record.args if '%' in str(record.msg) else str(record.msg)
                        else:
                            message = str(record.msg) % record.args if '%' in str(record.msg) else str(record.msg)
                    except (TypeError, ValueError):
                        # If formatting fails, just use the message as-is
                        message = str(record.msg)
                else:
                    message = str(record.msg)
            else:
                message = str(record)

            # Only log to RunPod logger if the length of the log entry is >= 1000 characters
            if len(message) <= 1000:
                level_mapping = {
                    logging.DEBUG: self.rp_logger.debug,
                    logging.INFO: self.rp_logger.info,
                    logging.WARNING: self.rp_logger.warn,
                    logging.ERROR: self.rp_logger.error,
                    logging.CRITICAL: self.rp_logger.error
                }

                # Wrapper to invoke RunPodLogger logging
                rp_logger = level_mapping.get(record.levelno, self.rp_logger.info)

                if runpod_job_id:
                    rp_logger(message, runpod_job_id)
                else:
                    rp_logger(message)

            if self.log_api_endpoint:
                try:
                    headers = {'Authorization': f'Bearer {self.log_token}'}

                    log_payload = {
                        'app_name': self.app_name,
                        'log_asctime': self.formatter.formatTime(record),
                        'log_levelname': record.levelname,
                        'log_message': message,
                        'runpod_endpoint_id': self.runpod_endpoint_id,
                        'runpod_cpu_count': self.runpod_cpu_count,
                        'runpod_pod_id': self.runpod_pod_id,
                        'runpod_gpu_size': self.runpod_gpu_size,
                        'runpod_mem_gb': self.runpod_mem_gb,
                        'runpod_gpu_count': self.runpod_gpu_count,
                        'runpod_volume_id': self.runpod_volume_id,
                        'runpod_pod_hostname': self.runpod_pod_hostname,
                        'runpod_debug_level': self.runpod_debug_level,
                        'runpod_dc_id': self.runpod_dc_id,
                        'runpod_gpu_name': self.runpod_gpu_name,
                        'runpod_job_id': runpod_job_id
                    }

                    response = requests.post(
                        self.log_api_endpoint,
                        json=log_payload,
                        headers=headers,
                        timeout=self.log_api_timeout
                    )

                    if response.status_code != 200:
                        self.rp_logger.error(f'Failed to send log to API. Status code: {response.status_code}')
                except requests.Timeout:
                    self.rp_logger.error(f'Timeout error sending log to API (timeout={self.log_api_timeout}s)')
                except Exception as e:
                    self.rp_logger.error(f'Error sending log to API: {str(e)}')
            else:
                self.rp_logger.warn('LOG_API_ENDPOINT environment variable is not set, not logging to API')
        except Exception as e:
            # Add error handling for message formatting
            self.rp_logger.error(f'Error in log formatting: {str(e)}')


# ---------------------------------------------------------------------------- #
#                               ComfyUI Functions                              #
# ---------------------------------------------------------------------------- #
def wait_for_service(url):
    retries = 0

    while True:
        try:
            requests.get(url)
            return
        except requests.exceptions.RequestException:
            retries += 1

            # Only log every 15 retries so the logs don't get spammed
            if retries % 15 == 0:
                logging.info('Service not ready yet. Retrying...')
        except Exception as err:
            logging.error(f'Error: {err}')

        time.sleep(0.2)


def send_get_request(endpoint):
    return session.get(
        url=f'{BASE_URI}/{endpoint}',
        timeout=TIMEOUT
    )


def send_post_request(endpoint, payload):
    return session.post(
        url=f'{BASE_URI}/{endpoint}',
        json=payload,
        timeout=TIMEOUT
    )


def get_txt2img_payload(workflow, payload):
    workflow["3"]["inputs"]["seed"] = payload["seed"]
    workflow["3"]["inputs"]["steps"] = payload["steps"]
    workflow["3"]["inputs"]["cfg"] = payload["cfg_scale"]
    workflow["3"]["inputs"]["sampler_name"] = payload["sampler_name"]
    workflow["4"]["inputs"]["ckpt_name"] = payload["ckpt_name"]
    workflow["5"]["inputs"]["batch_size"] = payload["batch_size"]
    workflow["5"]["inputs"]["width"] = payload["width"]
    workflow["5"]["inputs"]["height"] = payload["height"]
    workflow["6"]["inputs"]["text"] = payload["prompt"]
    workflow["7"]["inputs"]["text"] = payload["negative_prompt"]
    return workflow


def get_img2img_payload(workflow, payload):
    workflow["13"]["inputs"]["seed"] = payload["seed"]
    workflow["13"]["inputs"]["steps"] = payload["steps"]
    workflow["13"]["inputs"]["cfg"] = payload["cfg_scale"]
    workflow["13"]["inputs"]["sampler_name"] = payload["sampler_name"]
    workflow["13"]["inputs"]["scheduler"] = payload["scheduler"]
    workflow["13"]["inputs"]["denoise"] = payload["denoise"]
    workflow["1"]["inputs"]["ckpt_name"] = payload["ckpt_name"]
    workflow["2"]["inputs"]["width"] = payload["width"]
    workflow["2"]["inputs"]["height"] = payload["height"]
    workflow["2"]["inputs"]["target_width"] = payload["width"]
    workflow["2"]["inputs"]["target_height"] = payload["height"]
    workflow["4"]["inputs"]["width"] = payload["width"]
    workflow["4"]["inputs"]["height"] = payload["height"]
    workflow["4"]["inputs"]["target_width"] = payload["width"]
    workflow["4"]["inputs"]["target_height"] = payload["height"]
    workflow["6"]["inputs"]["text"] = payload["prompt"]
    workflow["7"]["inputs"]["text"] = payload["negative_prompt"]
    return workflow


def get_workflow_payload(workflow_name, payload):
    with open(f'/workflows/{workflow_name}.json', 'r') as json_file:
        workflow = json.load(json_file)

    if workflow_name == 'txt2img':
        workflow = get_txt2img_payload(workflow, payload)

    return workflow


def convert_image_to_jpeg(image_path, quality=JPEG_QUALITY):
    """
    Convert an image file to JPEG format and return base64 encoded data.
    
    Args:
        image_path (str): Path to the image file
        quality (int): JPEG quality (1-100, higher = better quality)
    
    Returns:
        str: Base64 encoded JPEG image data
    """
    try:
        # Open the image with PIL
        with Image.open(image_path) as img:
            # Convert RGBA to RGB if necessary (JPEG doesn't support transparency)
            if img.mode in ('RGBA', 'LA'):
                # Create a white background
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'RGBA':
                    background.paste(img, mask=img.split()[-1])  # Use alpha channel as mask
                else:
                    background.paste(img)
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Save as JPEG to a BytesIO buffer
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=quality, optimize=True)
            buffer.seek(0)
            
            # Encode to base64
            return base64.b64encode(buffer.getvalue()).decode('utf-8')
    except Exception as e:
        raise Exception(f"Failed to convert image {image_path} to JPEG: {str(e)}")


def get_output_files(output):
    """
    Get the output files (primarily images, as text files are usually just saved to disk)
    """
    files = []

    for key, value in output.items():
        # Handle image outputs
        if 'images' in value and isinstance(value['images'], list):
            for image in value['images']:
                files.append({
                    'type': 'image',
                    'data': image
                })
        
        # Note: Text outputs from SaveText nodes typically don't appear in the ComfyUI output structure
        # They are saved directly to disk and need to be found via filesystem scanning

    return files



def create_unique_filename_prefix(payload):
    """
    Create a unique filename prefix for each request to avoid a race condition where
    more than one request completes at the same time, which can either result in the
    incorrect output being returned, or the output file not being found.
    """
    for key, value in payload.items():
        class_type = value.get('class_type')

        # Handle image output nodes
        if class_type == 'SaveImage':
            payload[key]['inputs']['filename_prefix'] = str(uuid.uuid4())
        
        # Handle text output nodes (common ComfyUI text output node types)
        elif class_type in ['SaveText|pysssss', 'SaveText', 'TextFileOutput', 'WriteTextFile']:
            if 'filename_prefix' in payload[key]['inputs']:
                payload[key]['inputs']['filename_prefix'] = str(uuid.uuid4())
            elif 'file' in payload[key]['inputs']:
                # For SaveText|pysssss node, the filename field is called 'file'
                original_filename = payload[key]['inputs']['file']
                payload[key]['inputs']['file'] = f"{str(uuid.uuid4())}_{original_filename}"
            elif 'filename' in payload[key]['inputs']:
                # If there's a filename field, prepend the UUID
                original_filename = payload[key]['inputs']['filename']
                payload[key]['inputs']['filename'] = f"{str(uuid.uuid4())}_{original_filename}"


# ---------------------------------------------------------------------------- #
#                              Telemetry functions                             #
# ---------------------------------------------------------------------------- #
def get_container_memory_info(job_id=None):
    """
    Get memory information that's actually allocated to the container using cgroups.
    Returns a dictionary with memory stats in GB.
    Also logs the memory information directly.
    """
    try:
        mem_info = {}

        # First try to get host memory information as fallback
        try:
            with open('/proc/meminfo', 'r') as f:
                meminfo = f.readlines()

            for line in meminfo:
                if 'MemTotal:' in line:
                    mem_info['total'] = int(line.split()[1]) / (1024 * 1024)  # Convert from KB to GB
                elif 'MemAvailable:' in line:
                    mem_info['available'] = int(line.split()[1]) / (1024 * 1024)  # Convert from KB to GB
                elif 'MemFree:' in line:
                    mem_info['free'] = int(line.split()[1]) / (1024 * 1024)  # Convert from KB to GB

            # Calculate used memory (may be overridden by container-specific value below)
            if 'total' in mem_info and 'free' in mem_info:
                mem_info['used'] = mem_info['total'] - mem_info['free']
        except Exception as e:
            logging.warning(f"Failed to read host memory info: {str(e)}", job_id)

        # Try cgroups v2 path first (modern Docker)
        try:
            with open('/sys/fs/cgroup/memory.max', 'r') as f:
                max_mem = f.read().strip()
                if max_mem != 'max':  # If set to 'max', it means unlimited
                    mem_info['limit'] = int(max_mem) / (1024 * 1024 * 1024)  # Convert B to GB

            with open('/sys/fs/cgroup/memory.current', 'r') as f:
                mem_info['used'] = int(f.read().strip()) / (1024 * 1024 * 1024)  # Convert B to GB

        except FileNotFoundError:
            # Fall back to cgroups v1 paths (older Docker)
            try:
                with open('/sys/fs/cgroup/memory/memory.limit_in_bytes', 'r') as f:
                    mem_limit = int(f.read().strip())
                    # If the value is very large (close to 2^64), it's effectively unlimited
                    if mem_limit < 2**63:
                        mem_info['limit'] = mem_limit / (1024 * 1024 * 1024)  # Convert B to GB

                with open('/sys/fs/cgroup/memory/memory.usage_in_bytes', 'r') as f:
                    mem_info['used'] = int(f.read().strip()) / (1024 * 1024 * 1024)  # Convert B to GB

            except FileNotFoundError:
                # Try the third possible location for cgroups
                try:
                    with open('/sys/fs/cgroup/memory.limit_in_bytes', 'r') as f:
                        mem_limit = int(f.read().strip())
                        if mem_limit < 2**63:
                            mem_info['limit'] = mem_limit / (1024 * 1024 * 1024)  # Convert B to GB

                    with open('/sys/fs/cgroup/memory.usage_in_bytes', 'r') as f:
                        mem_info['used'] = int(f.read().strip()) / (1024 * 1024 * 1024)  # Convert B to GB

                except FileNotFoundError:
                    logging.warning('Could not find cgroup memory information', job_id)

        # Calculate available memory if we have both limit and used
        if 'limit' in mem_info and 'used' in mem_info:
            mem_info['available'] = mem_info['limit'] - mem_info['used']

        # Log memory information
        mem_log_parts = []
        if 'total' in mem_info:
            mem_log_parts.append(f"Total={mem_info['total']:.2f}")
        if 'limit' in mem_info:
            mem_log_parts.append(f"Limit={mem_info['limit']:.2f}")
        if 'used' in mem_info:
            mem_log_parts.append(f"Used={mem_info['used']:.2f}")
        if 'available' in mem_info:
            mem_log_parts.append(f"Available={mem_info['available']:.2f}")
        if 'free' in mem_info:
            mem_log_parts.append(f"Free={mem_info['free']:.2f}")

        if mem_log_parts:
            logging.info(f"Container Memory (GB): {', '.join(mem_log_parts)}", job_id)
        else:
            logging.info('Container memory information not available', job_id)

        return mem_info
    except Exception as e:
        logging.error(f'Error getting container memory info: {str(e)}', job_id)
        return {}


def get_container_cpu_info(job_id=None):
    """
    Get CPU information that's actually allocated to the container using cgroups.
    Returns a dictionary with CPU stats.
    Also logs the CPU information directly.
    """
    try:
        cpu_info = {}

        # First get the number of CPUs visible to the container
        try:
            # Count available CPUs by checking /proc/cpuinfo
            available_cpus = 0
            with open('/proc/cpuinfo', 'r') as f:
                for line in f:
                    if line.startswith('processor'):
                        available_cpus += 1
            if available_cpus > 0:
                cpu_info['available_cpus'] = available_cpus
        except Exception as e:
            logging.warning(f'Failed to get available CPUs: {str(e)}', job_id)

        # Try getting CPU quota and period from cgroups v2
        try:
            with open('/sys/fs/cgroup/cpu.max', 'r') as f:
                cpu_data = f.read().strip().split()
                if cpu_data[0] != 'max':
                    cpu_quota = int(cpu_data[0])
                    cpu_period = int(cpu_data[1])
                    # Calculate the number of CPUs as quota/period
                    cpu_info['allocated_cpus'] = cpu_quota / cpu_period
        except FileNotFoundError:
            # Try cgroups v1 paths
            try:
                with open('/sys/fs/cgroup/cpu/cpu.cfs_quota_us', 'r') as f:
                    cpu_quota = int(f.read().strip())
                with open('/sys/fs/cgroup/cpu/cpu.cfs_period_us', 'r') as f:
                    cpu_period = int(f.read().strip())
                if cpu_quota > 0:  # -1 means no limit
                    cpu_info['allocated_cpus'] = cpu_quota / cpu_period
            except FileNotFoundError:
                # Try another possible location
                try:
                    with open('/sys/fs/cgroup/cpu.cfs_quota_us', 'r') as f:
                        cpu_quota = int(f.read().strip())
                    with open('/sys/fs/cgroup/cpu.cfs_period_us', 'r') as f:
                        cpu_period = int(f.read().strip())
                    if cpu_quota > 0:
                        cpu_info['allocated_cpus'] = cpu_quota / cpu_period
                except FileNotFoundError:
                    logging.warning('Could not find cgroup CPU quota information', job_id)

        # Get container CPU usage stats
        try:
            # Try cgroups v2 path
            with open('/sys/fs/cgroup/cpu.stat', 'r') as f:
                for line in f:
                    if line.startswith('usage_usec'):
                        cpu_info['usage_usec'] = int(line.split()[1])
                        break
        except FileNotFoundError:
            # Try cgroups v1 path
            try:
                with open('/sys/fs/cgroup/cpu/cpuacct.usage', 'r') as f:
                    cpu_info['usage_usec'] = int(f.read().strip()) / 1000  # Convert ns to μs
            except FileNotFoundError:
                try:
                    with open('/sys/fs/cgroup/cpuacct.usage', 'r') as f:
                        cpu_info['usage_usec'] = int(f.read().strip()) / 1000
                except FileNotFoundError:
                    pass

        # Log CPU information
        cpu_log_parts = []
        if 'allocated_cpus' in cpu_info:
            cpu_log_parts.append(f"Allocated CPUs={cpu_info['allocated_cpus']:.2f}")
        if 'available_cpus' in cpu_info:
            cpu_log_parts.append(f"Available CPUs={cpu_info['available_cpus']}")
        if 'usage_usec' in cpu_info:
            cpu_log_parts.append(f"Usage={cpu_info['usage_usec']/1000000:.2f}s")

        if cpu_log_parts:
            logging.info(f"Container CPU: {', '.join(cpu_log_parts)}", job_id)
        else:
            logging.info('Container CPU allocation information not available', job_id)

        return cpu_info
    except Exception as e:
        logging.error(f'Error getting container CPU info: {str(e)}', job_id)
        return {}


def get_container_disk_info(job_id=None):
    """
    Get disk space information available to the container.
    Returns a dictionary with disk space stats.
    Also logs the disk space information directly.
    """
    try:
        disk_info = {}

        # Get disk usage statistics for the root (/) mount
        try:
            total, used, free = shutil.disk_usage('/')
            disk_info['total_bytes'] = total
            disk_info['used_bytes'] = used
            disk_info['free_bytes'] = free
            disk_info['usage_percent'] = (used / total) * 100
        except Exception as e:
            if job_id:
                logging.warning(f'Failed to get disk usage stats: {str(e)}', job_id)
            else:
                logging.warning(f'Failed to get disk usage stats: {str(e)}', job_id)

        # Try to get disk quota information from cgroups v2
        try:
            with open('/sys/fs/cgroup/io.stat', 'r') as f:
                content = f.read().strip()
                if content:
                    disk_info['io_stats_raw'] = content
        except FileNotFoundError:
            # Try cgroups v1
            try:
                with open('/sys/fs/cgroup/blkio/blkio.throttle.io_service_bytes', 'r') as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) >= 3 and 'Total' in line:
                            disk_info['io_bytes'] = int(parts[2])
                            break
            except FileNotFoundError:
                try:
                    with open('/sys/fs/cgroup/blkio.throttle.io_service_bytes', 'r') as f:
                        for line in f:
                            parts = line.strip().split()
                            if len(parts) >= 3 and 'Total' in line:
                                disk_info['io_bytes'] = int(parts[2])
                                break
                except FileNotFoundError:
                    if job_id:
                        logging.warning('Could not find cgroup disk I/O information', job_id)
                    else:
                        logging.warning('Could not find cgroup disk I/O information', job_id)

        # Get disk inodes information (important for container environments)
        try:
            import os
            stat = os.statvfs('/')
            disk_info['total_inodes'] = stat.f_files
            disk_info['free_inodes'] = stat.f_ffree
            disk_info['used_inodes'] = stat.f_files - stat.f_ffree
            if stat.f_files > 0:
                disk_info['inodes_usage_percent'] = ((stat.f_files - stat.f_ffree) / stat.f_files) * 100
        except Exception as e:
            if job_id:
                logging.warning(f'Failed to get inode information: {str(e)}', job_id)
            else:
                logging.warning(f'Failed to get inode information: {str(e)}', job_id)

        # Log disk information
        disk_log_parts = []
        if 'total_bytes' in disk_info:
            disk_log_parts.append(f"Total={disk_info['total_bytes']/(1024**3):.2f}GB")
        if 'used_bytes' in disk_info:
            disk_log_parts.append(f"Used={disk_info['used_bytes']/(1024**3):.2f}GB")
        if 'free_bytes' in disk_info:
            disk_log_parts.append(f"Free={disk_info['free_bytes']/(1024**3):.2f}GB")
        if 'usage_percent' in disk_info:
            disk_log_parts.append(f"Usage={disk_info['usage_percent']:.2f}%")
        if 'inodes_usage_percent' in disk_info:
            disk_log_parts.append(f"Inodes={disk_info['inodes_usage_percent']:.2f}%")
        if 'io_bytes' in disk_info:
            disk_log_parts.append(f"I/O={disk_info['io_bytes']/(1024**2):.2f}MB")

        if disk_log_parts:
            if job_id:
                logging.info(f"Container Disk: {', '.join(disk_log_parts)}", job_id)
            else:
                logging.info(f"Container Disk: {', '.join(disk_log_parts)}", job_id)
        else:
            if job_id:
                logging.info('Container disk space information not available', job_id)
            else:
                logging.info('Container disk space information not available', job_id)

        return disk_info
    except Exception as e:
        if job_id:
            logging.error(f'Error getting container disk info: {str(e)}', job_id)
        else:
            logging.error(f'Error getting container disk info: {str(e)}', job_id)
        return {}


# ---------------------------------------------------------------------------- #
#                                RunPod Handler                                #
# ---------------------------------------------------------------------------- #
def handler(event):
    job_id = event['id']
    os.environ['RUNPOD_JOB_ID'] = job_id

    try:
        memory_info = get_container_memory_info(job_id)
        cpu_info = get_container_cpu_info(job_id)
        disk_info = get_container_disk_info(job_id)

        memory_available_gb = memory_info.get('available')
        disk_free_bytes = disk_info.get('free_bytes')

        if memory_available_gb is not None and memory_available_gb < 0.5:
            raise Exception(f'Insufficient available container memory: {memory_available_gb:.2f} GB available (minimum 0.5 GB required)')

        if disk_free_bytes is not None and disk_free_bytes < DISK_MIN_FREE_BYTES:
            free_gb = disk_free_bytes / (1024**3)
            raise Exception(f'Insufficient free container disk space: {free_gb:.2f} GB available (minimum 0.5 GB required)')

        validated_input = validate(event['input'], INPUT_SCHEMA)

        if 'errors' in validated_input:
            return {
                'error': '\n'.join(validated_input['errors'])
            }

        payload = validated_input['validated_input']
        workflow_name = payload['workflow']
        payload = payload['payload']

        if workflow_name == 'default':
            workflow_name = 'txt2img'

        logging.info(f'Workflow: {workflow_name}', job_id)

        if workflow_name != 'custom':
            try:
                payload = get_workflow_payload(workflow_name, payload)
            except Exception as e:
                logging.error(f'Unable to load workflow payload for: {workflow_name}', job_id)
                raise

        create_unique_filename_prefix(payload)
        logging.debug('Queuing prompt', job_id)

        queue_response = send_post_request(
            'prompt',
            {
                'prompt': payload
            }
        )

        if queue_response.status_code == 200:
            resp_json = queue_response.json()
            prompt_id = resp_json['prompt_id']
            logging.info(f'Prompt queued successfully: {prompt_id}', job_id)
            retries = 0

            while True:
                # Only log every 15 retries so the logs don't get spammed
                if retries == 0 or retries % 15 == 0:
                    logging.info(f'Getting status of prompt: {prompt_id}', job_id)

                r = send_get_request(f'history/{prompt_id}')
                resp_json = r.json()

                if r.status_code == 200 and len(resp_json):
                    break

                time.sleep(0.2)
                retries += 1

            status = resp_json[prompt_id]['status']

            if status['status_str'] == 'success' and status['completed']:
                # Job was processed successfully
                outputs = resp_json[prompt_id]['outputs']

                logging.info(f'Files generated successfully for prompt: {prompt_id}', job_id)
                output_files = get_output_files(outputs)
                images = []
                text_files = []

                # Process image files from ComfyUI output structure
                for output_file in output_files:
                    if output_file['type'] == 'image':
                        filename = output_file['data'].get('filename')
                        file_type = output_file['data'].get('type')

                        if file_type == 'output':
                            image_path = f'{VOLUME_MOUNT_PATH}/ComfyUI/output/{filename}'

                            if os.path.exists(image_path):
                                # Log the image file size before conversion
                                image_size_bytes = os.path.getsize(image_path)
                                image_size_mb = image_size_bytes / (1024 * 1024)
                                logging.info(f'Output image size: {image_size_bytes} bytes ({image_size_mb:.2f} MB) for {image_path}', job_id)
                                # Convert image to JPEG and base64 encode
                                # Save JPEG to a temp buffer to get its size
                                try:
                                    with Image.open(image_path) as img:
                                        if img.mode in ('RGBA', 'LA'):
                                            background = Image.new('RGB', img.size, (255, 255, 255))
                                            if img.mode == 'RGBA':
                                                background.paste(img, mask=img.split()[-1])
                                            else:
                                                background.paste(img)
                                            img = background
                                        elif img.mode != 'RGB':
                                            img = img.convert('RGB')
                                        buffer = io.BytesIO()
                                        img.save(buffer, format='JPEG', quality=JPEG_QUALITY, optimize=True)
                                        jpeg_bytes = buffer.getvalue()
                                        jpeg_size_bytes = len(jpeg_bytes)
                                        jpeg_size_mb = jpeg_size_bytes / (1024 * 1024)
                                        logging.info(f'JPEG image size: {jpeg_size_bytes} bytes ({jpeg_size_mb:.2f} MB) for {image_path}', job_id)
                                        image_data = base64.b64encode(jpeg_bytes).decode('utf-8')
                                        images.append(image_data)
                                except Exception as e:
                                    logging.error(f'Error converting image to JPEG and logging size: {e}', job_id)
                                logging.info(f'Converted and encoded image to JPEG: {image_path}', job_id)
                                logging.info(f'Deleting output file: {image_path}', job_id)
                                os.remove(image_path)
                        elif file_type == 'temp':
                            image_path = f'{VOLUME_MOUNT_PATH}/ComfyUI/temp/{filename}'

                            # Clean up temp images that aren't used by the API
                            if os.path.exists(image_path):
                                logging.info(f'Deleting temp file: {image_path}', job_id)

                                try:
                                    os.remove(image_path)
                                except Exception as e:
                                    logging.error(f'Error deleting temp file {image_path}: {e}')
                            else:
                                # Check if the image exists in the /tmp directory
                                # NOTE: This is a specific workaround in a ComfyUI fork, and should
                                # not be present in the official ComfyUI Github repository.
                                image_path = f'/tmp/{filename}'

                                if os.path.exists(image_path):
                                    logging.info(f'Deleting temp file: {image_path}', job_id)

                                    try:
                                        os.remove(image_path)
                                    except Exception as e:
                                        logging.error(f'Error deleting temp file {image_path}: {e}')

                # Text files are saved directly to disk by SaveText nodes and need to be found via filesystem scanning
                # Extract unique prefix from the payload for better file matching
                unique_prefix = None
                for key, value in payload.items():
                    if isinstance(value, dict) and 'inputs' in value:
                        if 'filename_prefix' in value['inputs']:
                            unique_prefix = value['inputs']['filename_prefix']
                            break
                        elif 'file' in value['inputs'] and isinstance(value['inputs']['file'], str):
                            # For SaveText|pysssss node, extract the UUID prefix from the filename
                            filename = value['inputs']['file']
                            if '_' in filename:
                                unique_prefix = filename.split('_')[0]
                                break
                        elif 'filename' in value['inputs'] and isinstance(value['inputs']['filename'], str):
                            # Extract UUID prefix from filename
                            filename = value['inputs']['filename']
                            if '_' in filename:
                                unique_prefix = filename.split('_')[0]
                                break
                
                text_files = scan_for_text_files(job_id, unique_prefix)

                response = {
                    'images': images
                }
                
                # Add text files to response if any were generated
                if text_files:
                    response['text_files'] = text_files

                # Refresh worker if memory is low
                memory_info = get_container_memory_info(job_id)
                memory_available_gb = memory_info.get('available')

                if memory_available_gb is not None and memory_available_gb < 1.0:
                    logging.info(f'Low memory detected: {memory_available_gb:.2f} GB available, refreshing worker', job_id)
                    response['refresh_worker'] = True

                return response

            else:
                # Job did not process successfully
                for message in status['messages']:
                    key, value = message

                    if key == 'execution_error':
                        if 'node_type' in value and 'exception_message' in value:
                            node_type = value['node_type']
                            exception_message = value['exception_message']
                            raise RuntimeError(f'{node_type}: {exception_message}')
                        else:
                            # Log to file instead of RunPod because the output tends to be too verbose
                            # and gets dropped by RunPod logging
                            error_msg = f'Job did not process successfully for prompt_id: {prompt_id}'
                            logging.error(error_msg, job_id)
                            logging.info(f'{job_id}: Response JSON: {resp_json}', job_id)
                            raise RuntimeError(error_msg)

        else:
            try:
                queue_response_content = queue_response.json()
            except Exception as e:
                queue_response_content = str(queue_response.content)

            logging.error(f'HTTP Status code: {queue_response.status_code}', job_id)
            logging.error(queue_response_content, job_id)

            return {
                'error': f'HTTP status code: {queue_response.status_code}',
                'output': queue_response_content
            }
    except Exception as e:
        logging.error(f'An exception was raised: {e}', job_id)

        return {
            'error': traceback.format_exc(),
            'refresh_worker': True
        }


def scan_for_text_files(job_id, unique_prefix=None):
    """
    Scan output directories for text files saved by SaveText nodes.
    SaveText nodes save files directly to disk and don't appear in ComfyUI's output structure.
    """
    text_files = []
    
    # Common text file extensions
    text_extensions = ['.txt', '.json', '.xml', '.csv', '.log', '.md', '.yaml', '.yml']
    
    # Directories to scan
    scan_dirs = [
        f'{VOLUME_MOUNT_PATH}/ComfyUI/output',
        f'{VOLUME_MOUNT_PATH}/ComfyUI/temp',
        '/tmp'
    ]
    
    for scan_dir in scan_dirs:
        try:
            if os.path.exists(scan_dir):
                for filename in os.listdir(scan_dir):
                    file_path = os.path.join(scan_dir, filename)
                    
                    # Check if it's a text file
                    if any(filename.lower().endswith(ext) for ext in text_extensions):
                        # If we have a unique prefix, only process files with that prefix
                        if unique_prefix and not filename.startswith(unique_prefix):
                            continue
                            
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                                text_files.append({
                                    'filename': filename,
                                    'content': content
                                })
                                logging.info(f'Found and processed additional text file: {file_path}', job_id)
                                os.remove(file_path)
                        except Exception as e:
                            # Try reading as binary with UTF-8 decoding
                            try:
                                with open(file_path, 'rb') as f:
                                    content = f.read().decode('utf-8', errors='replace')
                                    text_files.append({
                                        'filename': filename,
                                        'content': content
                                    })
                                    logging.info(f'Found and processed additional text file (binary mode): {file_path}', job_id)
                                    os.remove(file_path)
                            except Exception as e2:
                                logging.error(f'Error processing additional text file {file_path}: {e2}', job_id)
        except Exception as e:
            logging.warning(f'Error scanning directory {scan_dir}: {e}', job_id)
    
    return text_files


def setup_logging():
    root_logger = logging.getLogger()
    root_logger.setLevel(LOG_LEVEL)

    # Remove all existing handlers from the root logger
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    formatter = logging.Formatter('%(asctime)s : %(levelname)s : %(message)s')
    log_handler = SnapLogHandler(APP_NAME)
    log_handler.setFormatter(formatter)
    root_logger.addHandler(log_handler)


if __name__ == '__main__':
    session = requests.Session()
    retries = Retry(total=10, backoff_factor=0.1, status_forcelist=[502, 503, 504])
    session.mount('http://', HTTPAdapter(max_retries=retries))
    setup_logging()
    wait_for_service(url=f'{BASE_URI}/system_stats')
    logging.info('ComfyUI API is ready')
    logging.info('Starting RunPod Serverless...')
    runpod.serverless.start(
        {
            'handler': handler
        }
    )
