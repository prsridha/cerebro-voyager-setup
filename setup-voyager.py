import os
import pwd
import json
import time
import fire
import shutil
import random
import subprocess
import oyaml as yaml
from pathlib import Path
from kubernetes import client, config


def run(cmd, shell=True, capture_output=True, text=True, halt_exception=True):
    try:
        out = subprocess.run(cmd, shell=shell, capture_output=capture_output, text=text)
        # print(cmd)
        if out.stderr:
            if halt_exception:
                raise Exception("Command Error:" + str(out.stderr))
            else:
                print("Command Error:" + str(out.stderr))
        if capture_output:
            return out.stdout.rstrip("\n")
        else:
            return None
    except Exception as e:
        print("Command Unsuccessful:", cmd)
        print(str(e))
        raise Exception


def wait_till_delete(namespace, label_selector, v1):
    def pod_exists():
        try:
            pods = v1.list_namespaced_pod(namespace, label_selector=label_selector)
            return len(pods.items) > 0
        except Exception as _:
            return False

    while pod_exists():
        time.sleep(2)


def create_rbac(namespace, username):
    # currently not being used
    # add RBAC to pods
    cmds = [
        "mkdir -p charts",
        "helm create charts/cerebro-rbac",
        "rm -rf charts/cerebro-rbac/templates/*",
        "cp misc/rbac.yaml charts/cerebro-rbac/templates/",
        "cp values.yaml charts/cerebro-rbac/values.yaml",
        "helm install --namespace={} {}-rbac charts/cerebro-rbac".format(namespace, username),
        "rm -rf charts"
    ]

    for cmd in cmds:
        time.sleep(0.5)
        run(cmd, capture_output=False)

    print("Role Based Access Controls created successfully")


class CerebroInstaller:
    def __init__(self):
        self.username = None
        self.namespace = None
        self.num_workers = None
        self.values_yaml = None

        # read values YAML file
        with open('values.yaml', 'r') as yaml_file:
            self.values_yaml = yaml.safe_load(yaml_file)

        updated = False
        username = run("whoami")

        # get username and update in values YAML file
        if "<username>" in self.values_yaml["cluster"]["username"]:
            updated = True
            self.values_yaml["cluster"]["username"] = self.values_yaml["cluster"]["username"].replace(
                "<username>", username)
            self.values_yaml["controller"]["volumes"]["baseHostPath"] = (
                self.values_yaml["controller"]["volumes"]["baseHostPath"].replace("<username>", username))
        if not (isinstance(self.values_yaml["cluster"]["userUID"], int) and isinstance(self.values_yaml["cluster"]["userGID"], int)):
            updated = True
            user_info = pwd.getpwnam(username)
            uid = user_info.pw_uid
            gid = user_info.pw_gid
            self.values_yaml["cluster"]["userUID"] = uid
            self.values_yaml["cluster"]["userGID"] = gid

        if updated:
            with open("values.yaml", "w") as f:
                yaml.safe_dump(self.values_yaml, f)

        # set commonly used values
        self.username = username
        self.namespace = self.values_yaml["cluster"]["namespace"]
        self.num_workers = self.values_yaml["cluster"]["numWorkers"]

    def _create_ports(self):
        def generate_random_ports(existing_ports):
            new_ports = set()
            while len(new_ports) < 2:
                port = random.randint(10001, 65535)
                if port not in existing_ports:
                    new_ports.add(port)
            return list(new_ports)

        v1 = client.CoreV1Api()
        configmap_name = "cerebro-ports"
        already_exists = False

        username = self.username

        try:
            api_response = v1.read_namespaced_config_map(name=configmap_name, namespace=self.namespace)
            data = api_response.data.get("cerebro-ports", "{}")
            configmap = json.loads(data) if data else {}
            already_exists = True
        except Exception:
            configmap = None
            print("ConfigMap {} not found".format(configmap_name))

        if configmap:
            existing_ports = {port for user_ports in configmap.values() for port in user_ports.values()}
        else:
            existing_ports = {}
        newports = generate_random_ports(existing_ports)

        # add username to configmap
        configmap[username] = {
            "jupyterNodePort": newports[0],
            "tensorboardNodePort": newports[1],
        }

        # republish configmap
        data = {username: ports for username, ports in configmap.items()}
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": configmap_name},
            "data": {"cerebro-ports": json.dumps(data)},
        }

        if already_exists:
            api_response = v1.patch_namespaced_config_map(name=configmap_name, namespace=self.namespace, body=body,
                                                          pretty=True)
        else:
            api_response = v1.create_namespaced_config_map(namespace=self.namespace, body=body, pretty=True)

        return configmap[username]

    def _get_ports(self):
        v1 = client.CoreV1Api()
        configmap_name = "cerebro-ports"

        try:
            api_response = v1.read_namespaced_config_map(name=configmap_name, namespace=self.namespace)
            data = api_response.data.get("cerebro-ports", "{}")
            configmap = json.loads(data) if data else {}
        except Exception:
            configmap = None
            print("ConfigMap {} not found".format(configmap_name))

        return configmap[self.username]

    def _delete_ports(self):
        v1 = client.CoreV1Api()
        configmap_name = "cerebro-ports"
        configmap = {}
        username = self.username

        try:
            api_response = v1.read_namespaced_config_map(name=configmap_name, namespace=self.namespace)
            data = api_response.data.get("cerebro-ports", "{}")
            print(data)
            configmap = json.loads(data) if data else {}
        except Exception as e:
            print(f"Error getting ConfigMap: {e}")
            return

        if username in configmap:
            del configmap[username]
            print(configmap)
            data = {username: ports for username, ports in configmap.items()}
            body = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": configmap_name},
                "data": {"cerebro-ports": json.dumps(data)},
            }
            api_response = v1.patch_namespaced_config_map(name=configmap_name, namespace=self.namespace, body=body,
                                                          pretty=True)

            return True
        else:
            return False

    def init_cerebro(self):
        config.load_kube_config()
        v1 = client.CoreV1Api()

        # create node hardware info configmap
        cm_exists = False
        try:
            _ = v1.read_namespaced_config_map(name="cerebro-node-hardware-info", namespace=self.namespace)
            print("Configmap for node hardware info already exists")
            cm_exists = True
        except Exception:
            pass

        if not cm_exists:
            node_hardware_info = {}
            for i in range(self.num_workers):
                node_hardware_info["node" + str(i)] = {
                    "num_cores": self.values_yaml["cluster"]["resourceRequests"]["workerCPU"],
                    "num_gpus": self.values_yaml["cluster"]["resourceRequests"]["workerGPU"]
                }
            configmap = client.V1ConfigMap(data={"data": json.dumps(node_hardware_info)},
                                           metadata=client.V1ObjectMeta(name="cerebro-node-hardware-info"))
            v1.create_namespaced_config_map(namespace=self.namespace, body=configmap)
            print("Created configmap for node hardware info")

        # create ports
        ports = self._create_ports()

        # make configmap of select values.yaml values
        configmap_values = {
            "username": self.username,
            "namespace": self.values_yaml["cluster"]["namespace"],
            "controller_data_path": self.values_yaml["controller"]["volumes"]["dataPath"],
            "user_code_path": self.values_yaml["controller"]["volumes"]["userCodePath"],
            "tensorboard_node_port": ports["tensorboardNodePort"],
            "shard_multiplicity": self.values_yaml["worker"]["shardMultiplicity"],
            "sample_size": self.values_yaml["worker"]["sampleSize"],
        }

        # create cerebro info configmap
        configmap = client.V1ConfigMap(data={"data": json.dumps(configmap_values)},
                                       metadata=client.V1ObjectMeta(name="{}-cerebro-info".format(self.username)))
        v1.create_namespaced_config_map(namespace=self.namespace, body=configmap)
        print("Created configmap for Cerebro values info")

        # create directories
        dirs = []
        base_path = self.values_yaml["controller"]["volumes"]["baseHostPath"].replace("<username>", self.username)
        dirs.append(os.path.join(base_path, self.values_yaml["controller"]["volumes"]["dataPath"].lstrip('/')))
        dirs.append(os.path.join(base_path, self.values_yaml["controller"]["volumes"]["metricsPath"].lstrip('/')))
        dirs.append(os.path.join(base_path, self.values_yaml["controller"]["volumes"]["checkpointPath"].lstrip('/')))
        dirs.append(os.path.join(base_path, self.values_yaml["controller"]["volumes"]["userCodePath"].lstrip('/')))
        for i in range(self.values_yaml["cluster"]["numWorkers"]):
            worker_name = "{}-cerebro-worker-{}".format(self.username, str(i))
            dirs.append(os.path.join(base_path, self.values_yaml["worker"]["workerDataPath"].lstrip('/'), worker_name))

        for i in dirs:
            Path(i).mkdir(parents=True, exist_ok=True)

    def create_controller(self):
        cmds = [
            "mkdir -p charts",
            "helm create charts/cerebro-controller",
            "rm -rf charts/cerebro-controller/templates/*",
            "cp ./controller/* charts/cerebro-controller/templates/",
            "cp values.yaml charts/cerebro-controller/values.yaml",
            "helm install --namespace={} {}-cerebro-controller charts/cerebro-controller/".format(self.namespace,
                                                                                                  self.username),
            "rm -rf charts"
        ]

        for cmd in cmds:
            time.sleep(0.5)
            run(cmd, capture_output=False)
        print("Created Controller deployment")

        config.load_kube_config()
        v1 = client.AppsV1Api()
        ready = False
        deployment_name = "{}-cerebro-controller".format(self.username)

        while not ready:
            rollout = v1.read_namespaced_deployment_status(name=deployment_name, namespace=self.namespace)
            if rollout.status.ready_replicas == rollout.status.replicas:
                print("Controller created successfully")
                ready = True
            else:
                time.sleep(1)

    def create_workers(self):
        # create ETL Workers
        cmds = [
            "mkdir -p charts",
            "helm create charts/cerebro-etl-worker",
            "rm -rf charts/cerebro-etl-worker/templates/*",
            "cp etl-worker/* charts/cerebro-etl-worker/templates/",
            "cp values.yaml charts/cerebro-etl-worker/values.yaml",
            "helm install --namespace={} {}-etl-worker charts/cerebro-etl-worker".format(self.namespace, self.username),
            "rm -rf charts"
        ]

        for cmd in cmds:
            time.sleep(0.5)
            run(cmd, capture_output=False)

        print("ETL Workers created successfully")

        # create MOP Workers
        cmds = [
            "mkdir -p charts",
            "helm create charts/cerebro-mop-worker",
            "rm -rf charts/cerebro-mop-worker/templates/*",
            "cp mop-worker/* charts/cerebro-mop-worker/templates/",
            "cp values.yaml charts/cerebro-mop-worker/values.yaml",
            "helm install --namespace={} {}-mop-worker charts/cerebro-mop-worker".format(self.namespace, self.username),
            "rm -rf charts"
        ]

        for cmd in cmds:
            time.sleep(0.5)
            run(cmd, capture_output=False)

        print("MOP Workers created successfully")

    def shutdown_cerebro(self):
        # load kubernetes config
        config.load_kube_config()
        v1 = client.CoreV1Api()

        # clean up Workers
        try:
            cmd1 = "helm delete {}-etl-worker -n {}".format(self.username, self.namespace)
            run(cmd1, capture_output=False)
            cmd2 = "helm delete {}-mop-worker -n {}".format(self.username, self.namespace)
            run(cmd2, capture_output=False)

            etl_label_selector = "app=cerebro-etl-worker,user={}".format(self.username)
            mop_label_selector = "app=cerebro-mop-worker,user={}".format(self.username)
            wait_till_delete(self.namespace, etl_label_selector, v1)
            wait_till_delete(self.namespace, mop_label_selector, v1)

        except Exception as _:
            print("Got an error while cleaning up Workers")

        print("Cleaned up Workers")

        # clean up Controller
        try:
            cmd4 = "helm delete {}-cerebro-controller -n {}".format(self.username, self.namespace)
            run(cmd4, halt_exception=False)
            label_selector = "app=cerebro-controller,user={}".format(self.username)
            wait_till_delete(self.namespace, label_selector, v1)
        except Exception as e:
            print("Got error while cleaning up Controller: " + str(e))
        print("Cleaned up Controller")

        # cleanUp ConfigMaps
        configmap_name = "{}-cerebro-info".format(self.username)
        try:
            # Delete the ConfigMap
            v1.delete_namespaced_config_map(
                name=configmap_name,
                namespace=self.namespace,
                body=client.V1DeleteOptions(),
            )
            print(f"ConfigMap '{configmap_name}' deleted successfully.")
        except Exception as e:
            print(f"Error deleting ConfigMap '{configmap_name}': {e}")

        # clear out hostPath Volumes
        base_path = self.values_yaml["controller"]["volumes"]["baseHostPath"].replace("<username>", self.username)
        shutil.rmtree(base_path)

    def testing(self):
        pass

    def print_url(self):
        # generate ssh command

        j = self.values_yaml["cluster"]["jupyterTokenSting"]
        j_bin = j.encode("utf-8")
        j_token = j_bin.hex().upper()
        j_port = self._get_ports()["jupyterNodePort"]
        jupyter_url = "http://localhost:" + str(j_port) + "/?token=" + j_token

        print("You can access the cluster using this URL:")
        print(jupyter_url)

    def install_cerebro(self):
        # initialize basic cerebro components
        self.init_cerebro()

        # creates Controller
        self.create_controller()

        # create Workers
        self.create_workers()

        time.sleep(5)
        self.print_url()


if __name__ == '__main__':
    fire.Fire(CerebroInstaller)
