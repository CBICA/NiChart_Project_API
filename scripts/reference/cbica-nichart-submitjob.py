import json
import os
import boto3
import yaml
from pathlib import Path
from typing import Dict, List, Union, Optional
from pydantic import BaseModel, Field, validator
from pathlib import Path
import subprocess
import json
from botocore.exceptions import ClientError
import jwt
import urllib.request
from io import BytesIO

BATCH_QUEUE = "cbica-nichart-jobqueue-standard"
BATCH_JOB_DEF = "cbica-nichart-jobdefinition-template1"

S3_BUCKET = "cbica-nichart-staticfiles"
S3_TOOL_PREFIX = "tools/"  # all tools stored under tools/{tool_name}.yaml

DEFAULT_TOOL_DEFINITION_PATH = Path(__file__).parent.parent.parent.parent / "resources/tools/"


COGNITO_POOL_ID = "us-east-1_BSBhcKA66"
COGNITO_APP_CLIENT_ID = "4shr6mm2h0p0i4o9uleqpu33fj"
REGION = "us-east-1"
#JWKS_URL = f"https://cognito-idp.{REGION}.amazonaws.com/{COGNITO_POOL_ID}/.well-known/jwks.json"

ASG_NAME = "cbica-nichart-unmanaged-ASG"
COMPUTE_ENV_NAME = "cbica-nichart-compute-environment-unmanaged"

autoscaling = boto3.client("autoscaling")
ec2 = boto3.client("ec2")
batch = boto3.client("batch")

# cache keys
_jwk_cache = {}

def get_jwk_keys(region, kid):
    try:

        jwks_url = f"https://public-keys.auth.elb.{region}.amazonaws.com/{kid}"
        global _jwk_cache
        if not _jwk_cache:
            with urllib.request.urlopen(jwks_url) as response:
                _jwk_cache = response.read().decode('utf-8')
        return _jwk_cache
    except Exception as e:
        print(f"Error fetching public key: {e}")
        raise

def verify_alb_token_and_get_user_id(token: str):
    try:
        token_bytes = token.encode('utf-8')
        payload = jwt.decode(token, options={"verify_signature": False})
        unverified_header = jwt.get_unverified_header(token)

        region = "us-east-1"
        jwks_url = f"https://public-keys.auth.elb.{region}.amazonaws.com/{unverified_header['kid']}"
        req = urllib.request.Request(jwks_url)

        try:
            with urllib.request.urlopen(req) as response:
                public_key_pem = response.read().decode('utf-8')

                decoded = jwt.decode(
                    token,
                    public_key_pem,
                    algorithms=['ES256'],
                    options={
                        'verify_signature': False,
                        'verify_exp': False
                        }
                )

                return decoded['sub']
        except urllib.error.URLError as e:
            print(f"Error accessing ALB public key: {e}")
            return verify_cognito_token(token, payload['iss'])
        
        decoded = jwt.decode(token_bytes, public_key, algorithms=['RS256', 'ES256'], options={
            'verify_signature': False,
            "verify_exp": False}
            )
        return decoded["sub"]
    except Exception as e:
        print(f"Error verifying token: {e}")
        print(f"Token: {token[:20]}...")
        print(f"Header: {unverified_header if 'unverified_header' in locals() else 'Not available'}")
        raise

def verify_cognito_token(token, issuer):
    '''Fallback method to verify with Cognito JWKS'''
    print(f"Attempting Cognito verification with issuer: {issuer}")

    jwks_url = f"{issuer}/.well_known/jwks.json"
    with urllib.request.urlopen(jwks_url) as response:
        jwks = json.load(response)
    
    kid = jwt.get_unverified_header(token)['kid']
    key = next((k for k in jwks['keys'] if k['kid'] == kid), None)
    if not key:
        raise Exception("Public key not found in Cognito JWKS")
    
    public_key = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(key))

    decoded = jwt.decode(
        token,
        public_key,
        algorithms=['ES256'],
        options={'verify_exp': True}
    )

    return decoded['sub']

def is_safe_path(base_dir: Union[str, Path], target_path: Union[str, Path]) -> bool:
    """
    Check that the target_path is a subpath of base_dir and doesn't escape it via symlinks or traversal.
    
    Parameters:
        base_dir: The base directory within which target_path must reside.
        target_path: The user-supplied path to validate.
        
    Returns:
        True if the path is valid and contained within base_dir, False otherwise.
    """
    base_dir = Path(base_dir).resolve(strict=False)
    target_path = Path(target_path).resolve(strict=False)

    try:
        target_path.relative_to(base_dir)
        return True
    except ValueError:
        return False

class IOField(BaseModel):
    type: str  # "file" or "directory"
    description: Optional[str] = None


class MountConfig(BaseModel):
    path_in_container: str
    mode: str = Field("ro", pattern="^(ro|rw)$")


class ResourceSpec(BaseModel):
    vcpus: int = 4
    memory: int = 16000 # e.g., 16GB -> 16000 
    gpus: int = 0


class ParameterSpec(BaseModel):
    type: str  # "int", "float", "bool", "str"
    default: Optional[Union[int, float, bool, str]] = None
    choices: Optional[List[Union[int, float, str]]] = None


class ToolSpec(BaseModel):
    name: str
    description: Optional[str]
    inputs: Dict[str, IOField]
    outputs: Dict[str, IOField]
    mounts: Dict[str, MountConfig]
    resources: ResourceSpec
    container: Dict[str, Union[str, List[str]]]
    parameters: Dict[str, ParameterSpec]

    def validate_params(self, user_params: Dict[str, Union[int, float, bool, str]]) -> Dict[str, Union[int, float, bool, str]]:
        validated = {}
        for key, spec in self.parameters.items():
            if key in user_params:
                value = user_params[key]
            elif spec.default is not None:
                value = spec.default
            else:
                raise ValueError(f"Missing required parameter: {key}")

            # Type check
            if spec.type == "int" and not isinstance(value, int):
                raise TypeError(f"Parameter {key} must be int")
            elif spec.type == "float" and not isinstance(value, float):
                raise TypeError(f"Parameter {key} must be float")
            elif spec.type == "bool" and not isinstance(value, bool):
                raise TypeError(f"Parameter {key} must be bool")
            elif spec.type == "str" and not isinstance(value, str):
                raise TypeError(f"Parameter {key} must be str")

            # Choice check
            if spec.choices and value not in spec.choices:
                raise ValueError(f"Invalid value for {key}. Must be one of {spec.choices}")

            validated[key] = value
        return validated


    def generate_docker_command(self, param_values: Dict[str, Union[int, float, bool, str]], mount_paths: Dict[str, str]) -> str:
        # This line will throw if validation fails
        param_values = self.validate_params(param_values)

        # Construct any default docker args here
        print("DEBUG: constructing global docker args")
        global_docker_args = ['--rm', '--ipc=host']
        if self.resources.gpus != 0:
            global_docker_args.append('--gpus all')

        # Apply substitution for command template
        print("DEBUG: command template substitution")
        command_template = self.container["command"]
        for key, mount in self.mounts.items():
            command_template = command_template.replace(f"{{{key}}}", mount.path_in_container)

        for key, val in param_values.items():
            command_template = command_template.replace(f"{{{key}}}", str(val))

        # Generate mount arguments
        mount_args = []
        
        print("DEBUG: Adding static mounts")
        ## TODO: Fix this hardcoded crap (see additional note in lambda_handler). 
        # In lambda_handler we append static s3 sync commands to pull models to the instance, here we handle mounting them into the task container
        # We assume that /mounted-models exists on the host machine (requires us to mount this in batch job def and in cloud-init script)
        static_mount_args = []
        if self.name == "nichart_dkgp":
            print("DEBUG: Detected nichart_dkgp during mount generation.")
            print("DEBUG: Adding DKGP-specific static mounts from /mounted-models")
            static_mount_args.append("-v /mounted-models/dkgp/models:/app/models:ro")
            static_mount_args.append("-v /mounted-models/dkgp/models_spare:/app/models_spare:ro")
            static_mount_args.append("-v /mounted-models/dkgp/models_cognitive:/app/models_cognitive:ro")
        
        print("DEBUG: Iterating over mounts...")
        for label, config in self.mounts.items():
            print(f"DEBUG: Processing mount label: {label}")
            print(f"DEBUG: Mount config: {config}")
            print(f"DEBUG: Mount paths: {mount_paths}")
            host_path = mount_paths[label]
            print(f"DEBUG: Host path: {host_path}")
            print(f"DEBUG: Checking mount type status...")
            mode = config.mode
            is_output = False
            is_ofile = False
            is_ifile = False
            if label in self.outputs:
                print(f"DEBUG: Label {label} found in outputs")
                is_output = True
                if self.outputs[label].type == 'file':
                    is_ofile = True
            elif label in self.inputs: # necessarily this is an input
                print(f"DEBUG: Label {label} found in inputs")
                if self.inputs[label].type == 'file':
                    is_ifile = True
            else:
                print(f"Mount label {label} not found in tool spec inputs or outputs.")
                raise ValueError(f"Mount label {label} not found in tool spec inputs or outputs.")
            print(f"DEBUG: is_output: {is_output}, is_ofile: {is_ofile}, is_ifile: {is_ifile}")
            print(f"DEBUG: Resolving mount type configuration...")
            if is_ofile: # Need to create parent dir of output location
                parent_host_path = Path(host_path).parent.resolve()
                parent_container_path = Path(config.path_in_container).parent
                mount_args.append(f"-v {parent_host_path}:{parent_container_path}:{mode}")
            elif is_ifile: # Need to mount file directly in
                mount_args.append(f"--mount type=bind,source={host_path},target={config.path_in_container}")
            else: # General case handling dirs
                mount_args.append(f"-v {host_path}:{config.path_in_container}:{mode}")
        print("DEBUG: final docker command construction")
        docker_cmd = f"docker pull {self.container['image']} && docker run {' '.join(global_docker_args)} {' '.join(static_mount_args)} {' '.join(mount_args)} {self.container['image']} {command_template}"
        print(f"Returning the following docker command: {docker_cmd}")

        return docker_cmd

    def get_container_image(self):
        return self.container['image']

def load_tool_spec_from_yaml(yaml_path: Union[str, Path]) -> ToolSpec:
    with open(yaml_path, 'r') as f:
        raw_data = yaml.safe_load(f)
    return ToolSpec(**raw_data)

def ensure_and_validate_mount_paths(
    mount_paths: Dict[str, str],
    base_dir: Union[str, Path],
    input_labels: set,
    output_labels: set
) -> None:
    """
    Validate that input paths exist and are within the requested root dir.
    For outputs, ensure parent directories exist (creating them if needed).
    
    Parameters:
        mount_paths: Dictionary of host paths keyed by label.
        base_dir: Root directory to constrain access.
        input_labels: Set of input labels.
        output_labels: Set of output labels.
    
    Raises:
        FileNotFoundError or ValueError for unsafe or invalid paths.
    """
    base_dir = Path(base_dir).resolve(strict=False)

    for label, path in mount_paths.items():
        resolved = Path(path).resolve(strict=False)

        if not is_safe_path(base_dir, resolved):
            raise ValueError(f"Unsafe mount path for label '{label}': {resolved} escapes {base_dir}")

        if label in input_labels:
            if not resolved.exists():
                #raise FileNotFoundError(f"Input path does not exist for '{label}': {resolved}")
                pass

        elif label in output_labels:
            parent = resolved.parent
            if not parent.exists():
                #print(f"Creating parent directory for output path '{label}': {parent}")
                #parent.mkdir(parents=True, exist_ok=True)
                pass

def validate_user_request(tool_name: str, user_params: Dict, user_mounts: Dict[str, str], tool_registry_path: Union[str, Path]) -> str:

    tool_spec = load_tool_spec_from_s3(tool_name)
    
    input_labels = set(tool_spec.inputs.keys())
    output_labels = set(tool_spec.outputs.keys())

    ensure_and_validate_mount_paths(user_mounts, base_mount_dir, input_labels, output_labels)

    return tool_spec.generate_docker_command(user_params, user_mounts)

def load_tool_spec_from_s3(tool_name: str) -> ToolSpec:
    s3 = boto3.client("s3")
    key = f"{S3_TOOL_PREFIX}{tool_name}.yaml"
    try:
        response = s3.get_object(Bucket=S3_BUCKET, Key=key)
        content = response["Body"].read()
        data = yaml.safe_load(BytesIO(content))
        return ToolSpec(**data)
    except Exception as e:
        raise FileNotFoundError(f"Tool '{tool_name}' not found in S3: {e}")

def increment_asg(current, max_val):
    if current < max_val:
        autoscaling.update_auto_scaling_group(
            AutoScalingGroupName = ASG_NAME,
            DesiredCapacity= current + 1
        )
        print(f"Incremented ASG desired capacity to {current + 1}")
    else:
        print("ASG already at max capacity. No scale-up.")

def lambda_handler(event, context):
    try:
        # Get user identity
        print("DEBUG: getting id from token")
        user_id = verify_alb_token_and_get_user_id(event["id_token"])
        if not user_id:
            return {"statusCode": 403, "body": json.dumps({"error": "Unauthorized or missing user ID"})}

        print("DEBUG: Reading request payload")
        print(f"DEBUG: Event body: {event}")
 
        if isinstance(event, str):
            body = json.loads(body)
        else:
            body = event

        tool_name = body["tool_name"]
        user_params = body["user_params"]
        user_mounts = body["user_mounts"]
        num_subjects = body.get("num_subjects", 0)
        print("DEBUG: loading tool spec")
        tool = load_tool_spec_from_s3(tool_name)

        # Set a per-user base dir
        base_mount_dir = f"/fsx/fsx/{user_id}/"
        print("DEBUG: entering mount path validation")
        ensure_and_validate_mount_paths(
            mount_paths=user_mounts,
            base_dir=base_mount_dir,
            input_labels=set(tool.inputs.keys()),
            output_labels=set(tool.outputs.keys())
        )

        # Check ASG/Batch, scale up ASG pre-emptively if needed
        
        asg = autoscaling.describe_auto_scaling_groups(
            AutoScalingGroupNames=[ASG_NAME]
        )["AutoScalingGroups"][0]

        desired = asg["DesiredCapacity"]
        max_size = asg["MaxSize"]

        instance_ids = [i["InstanceId"] for i in asg["Instances"] if i["LifecycleState"] == "InService"]
        if not instance_ids:
            print(f"No in-service instances detected in ASG {ASG_NAME}, scaling up...")
            increment_asg(desired, max_size)
        else:
            ec2_states = ec2.describe_instance_status(InstanceIds=instance_ids)["InstanceStatuses"]
            running_ids = [i["InstanceId"] for i in ec2_states if i["InstanceState"]["Name"] == "running"]

            compute_env = batch.describe_compute_environments(
                computeEnvironments=[COMPUTE_ENV_NAME]
            )["computeEnvironments"][0]

            busy_count = compute_env.get("computeResources", {}).get("desiredvCpus", 0)
            min_vcpus = compute_env.get("computeResources", {}).get("minvCpus", 0)

            if len(running_ids) == 0 or busy_count >= len(running_ids):
                print("All instances busy or none running, scaling up...")
            else:
                print("At least one idle instance detected, no scaling needed.")


        print("DEBUG: generating docker command")
        docker_command = tool.generate_docker_command(user_params, user_mounts)
        print(f"DEBUG: submitting to batch with command: {docker_command}")
        
        # Build resource requirements dynamically
        resource_requirements = [
            {
                'type': 'VCPU',
                'value': str(tool.resources.vcpus),
            },
            {
                'type': 'MEMORY',
                'value': str(tool.resources.memory),
            }
        ]

        # Only add GPU requirement if it's greater than 0
        if tool.resources.gpus > 0:
            resource_requirements.append({
                'type': 'GPU',
                'value': str(tool.resources.gpus),
            })
        
        # Build additional prefix commands for the manager container to run as setup before task container job
        extra_prefix_commands_list = []
        
        ## TODO: Fix this hardcoded ad-hoc crap (here and in docker-command-gen)
        # At least this solution prevents us from needing to s3 sync models for other job types. Only works if the docker command gen generates the proper mount and the dkgp container expects the in-container paths
        print(f"Current tool name: {tool_name}")
        if tool_name == "dkgp":
            print(f"Detected DKGP. Prepending model-pull commands to the job.")
            extra_prefix_commands_list.append("mkdir -pv /mounted-models/dkgp") # Ensure destination exists
            extra_prefix_commands_list.append("aws s3 sync s3://cbica-nichart-privatemodels/dkgp /mounted-models/dkgp --delete")
            
        print(f"Extra prefix commands: {extra_prefix_commands_list}")
        # Finalize prefix commands string
        extra_prefix_commands_str = ' && '.join(extra_prefix_commands_list) if extra_prefix_commands_list else 'echo placeholder'
        print(f"Extra prefix commands string: {extra_prefix_commands_str}")
        response = batch.submit_job(
            jobName=f"{tool_name}-{user_id}",
            jobQueue=BATCH_QUEUE,
            jobDefinition=BATCH_JOB_DEF,
            #containerOverrides={
            #    "command": ["bash", "-c", docker_command],
            #    "environment": [{"name": "USER_ID", "value": user_id}]
            #},
            ecsPropertiesOverride={
                'taskProperties': [
                    {
                        'containers': [
                            {
                                'name': 'fsx-manager',
                                'command': ['bash', '-c', f'docker pull {tool.get_container_image()} && aws s3 sync s3://cbica-nichart-io/fsx/{user_id} /fsx/fsx/{user_id} --delete && {extra_prefix_commands_str} && {docker_command} && aws s3 sync /fsx/fsx/{user_id} s3://cbica-nichart-io/fsx/{user_id}'],
                                'resourceRequirements': resource_requirements
                            }
                        ]
                    }
                ]
            },
            parameters={
                'FullCommand': docker_command,
                'ContainerImage': tool.get_container_image(),
                'tool_id': tool_name,
                'num_subjects': str(num_subjects),
            },
            tags={
                "submitted_by": user_id,
                "tool": tool_name
            }
        )

        result = {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Job submitted successfully",
                "job_id": response["jobId"],
                "job_name": tool.name,
            })
        }
        print("DEBUG: Result:")
        print(result)

        return result

    except Exception as e:
        result = {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)})
        }
        print("DEBUG: Error Result:")
        print(result)
        return result