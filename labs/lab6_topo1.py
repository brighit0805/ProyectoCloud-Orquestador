import os
import sys
import json
import time
import re
import requests
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# ============================== Initialization ===================================
load_dotenv()

KEYSTONE_PORT       = os.getenv("KEYSTONE_PORT",       "55000")
NOVA_PORT           = os.getenv("NOVA_PORT",           "58774")
NEUTRON_PORT        = os.getenv("NEUTRON_PORT",        "59696")
GLANCE_PORT         = os.getenv("GLANCE_PORT",         "59292")
ACCESS_NODE_IP      = os.getenv("ACCESS_NODE_IP",      "localhost")
ADMIN_USER_ID       = os.getenv("ADMIN_USER_ID",       "")
ADMIN_USER_PASSWORD = os.getenv("ADMIN_USER_PASSWORD", "")
DOMAIN_ID           = os.getenv("DOMAIN_ID",           "")
ADMIN_PROJECT_ID    = os.getenv("ADMIN_PROJECT_ID",    "")
ADMIN_ROLE_ID       = os.getenv("ADMIN_ROLE_ID",       "")
COMPUTE_API_VERSION = os.getenv("COMPUTE_API_VERSION", "2.87")

KEYSTONE_ENDPOINT = f"http://{ACCESS_NODE_IP}:{KEYSTONE_PORT}/v3"
NOVA_ENDPOINT     = f"http://{ACCESS_NODE_IP}:{NOVA_PORT}/v2.1"
NEUTRON_ENDPOINT  = f"http://{ACCESS_NODE_IP}:{NEUTRON_PORT}/v2.0"
GLANCE_ENDPOINT   = f"http://{ACCESS_NODE_IP}:{GLANCE_PORT}/v2"  # Glance uses /v2 (not /v2.0)

print("Endpoints:")
print("  KEYSTONE :", KEYSTONE_ENDPOINT)
print("  NOVA     :", NOVA_ENDPOINT)
print("  NEUTRON  :", NEUTRON_ENDPOINT)
print("  GLANCE   :", GLANCE_ENDPOINT)
print()
missing = [k for k, v in {
    "ACCESS_NODE_IP": ACCESS_NODE_IP, "DOMAIN_ID": DOMAIN_ID,
    "ADMIN_PROJECT_ID": ADMIN_PROJECT_ID, "ADMIN_USER_ID": ADMIN_USER_ID,
    "ADMIN_USER_PASSWORD": ADMIN_USER_PASSWORD, "ADMIN_ROLE_ID": ADMIN_ROLE_ID,
}.items() if not v]
if missing:
    print("!  Missing variables:", ", ".join(missing))
else:
    print("OK. Credentials loaded.")


# ============================== Functions ===================================

# ==========================================================================
# Keystone
# ==========================================================================
def password_authentication_with_scoped_authorization(auth_endpoint, user_id, password, domain_id, project_id):
    """POST /auth/tokens — Password-based authentication scoped to a project. Success = 201."""
    url = auth_endpoint + "/auth/tokens"
    data = {"auth": {
        "identity": {"methods": ["password"],
                     "password": {"user": {"id": user_id,
                                           "domain": {"id": domain_id},
                                           "password": password}}},
        "scope": {"project": {"domain": {"id": domain_id}, "id": project_id}},
    }}
    return requests.post(url=url, data=json.dumps(data),
                         headers={"Content-type": "application/json"})

def token_authentication_with_scoped_authorization(auth_endpoint, token, domain_id, project_id):
    """POST /auth/tokens — Re-scoping from existing token. Success = 201."""
    url = auth_endpoint + "/auth/tokens"
    data = {"auth": {
        "identity": {"methods": ["token"], "token": {"id": token}},
        "scope": {"project": {"domain": {"id": domain_id}, "id": project_id}},
    }}
    return requests.post(url=url, data=json.dumps(data),
                         headers={"Content-type": "application/json"})

def create_project(auth_endpoint, token, domain_id, project_name, project_description=""):
    """POST /projects — Project creation. Success = 201."""
    url = auth_endpoint + "/projects"
    headers = {"Content-type": "application/json", "X-Auth-Token": token}
    data = {"project": {"name": project_name,
                        "description": project_description,
                        "domain_id": domain_id}}
    return requests.post(url=url, headers=headers, data=json.dumps(data))

def create_user(auth_endpoint, token, domain_id, user_name, password,
                default_project_id=None, enabled=True):
    """POST /users — User creation. Success = 201."""
    url = auth_endpoint + "/users"
    headers = {"Content-type": "application/json", "X-Auth-Token": token}
    user = {"name": user_name, "domain_id": domain_id,
            "password": password, "enabled": enabled}
    if default_project_id:
        user["default_project_id"] = default_project_id
    return requests.post(url=url, headers=headers, data=json.dumps({"user": user}))

def assign_role_to_user_on_project(auth_endpoint, token, project_id, user_id, role_id):
    """PUT /projects/{p}/users/{u}/roles/{r} — Role assignment. Success = 204."""
    url = (auth_endpoint + "/projects/" + project_id +
           "/users/" + user_id + "/roles/" + role_id)
    headers = {"Content-type": "application/json", "X-Auth-Token": token}
    return requests.put(url=url, headers=headers)


# ==========================================================================
# Nova
# ==========================================================================
def create_server(nova_endpoint, token, name, flavor_id, image_id, networks=None):
    """POST /servers — Instance creation. Success = 202 (async)."""
    url = nova_endpoint + "/servers"
    headers = {"Content-type": "application/json", "X-Auth-Token": token}
    data = {"server": {"name": name, "flavorRef": flavor_id,
                       "imageRef": image_id, "networks": networks}}
    return requests.post(url=url, headers=headers, data=json.dumps(data))

def get_server_console(nova_endpoint, token, server_id, compute_api_version):
    """POST /servers/{id}/remote-consoles — noVNC URL. Success = 200."""
    url = nova_endpoint + "/servers/" + server_id + "/remote-consoles"
    headers = {"Content-type": "application/json", "X-Auth-Token": token,
               "OpenStack-API-Version": "compute " + compute_api_version}
    data = {"remote_console": {"protocol": "vnc", "type": "novnc"}}
    return requests.post(url=url, headers=headers, data=json.dumps(data))


# ==========================================================================
# Neutron
# ==========================================================================
def create_network(auth_endpoint, token, name,
                   shared=False, provider_network_type=None,
                   provider_physical_network=None, router_external=False,
                   port_security_enabled=False):
    """POST /networks — Network creation. For provider: shared=True,
    provider_network_type='flat', provider_physical_network='<mapping>'.
    Success = 201."""
    url = auth_endpoint + "/networks"
    headers = {"Content-type": "application/json", "X-Auth-Token": token}
    network = {"name": name, "port_security_enabled": bool(port_security_enabled)}
    if shared:
        network["shared"] = True
    if provider_network_type:
        network["provider:network_type"] = provider_network_type
    if provider_physical_network:
        network["provider:physical_network"] = provider_physical_network
    if router_external:
        network["router:external"] = True
    return requests.post(url=url, headers=headers, data=json.dumps({"network": network}))

def create_subnet(auth_endpoint, token, network_id, name, ip_version, cidr,
                  enable_dhcp=False, gateway_ip=None,
                  allocation_pools=None, dns_nameservers=None):
    """POST /subnets — Subnet creation. Success = 201."""
    url = auth_endpoint + "/subnets"
    headers = {"Content-type": "application/json", "X-Auth-Token": token}
    subnet = {"network_id": network_id, "name": name,
              "enable_dhcp": bool(enable_dhcp), "ip_version": int(ip_version),
              "cidr": cidr, "gateway_ip": gateway_ip}
    if allocation_pools is not None:
        subnet["allocation_pools"] = allocation_pools
    if dns_nameservers is not None:
        subnet["dns_nameservers"] = dns_nameservers
    return requests.post(url=url, headers=headers, data=json.dumps({"subnet": subnet}))

def create_port(auth_endpoint, token, name, network_id, project_id,
                fixed_ips=None, port_security_enabled=False):
    """POST /ports — Port creation. Success = 201."""
    url = auth_endpoint + "/ports"
    headers = {"Content-type": "application/json", "X-Auth-Token": token}
    port = {"name": name, "tenant_id": project_id, "network_id": network_id,
            "port_security_enabled": bool(port_security_enabled)}
    if fixed_ips is not None:
        port["fixed_ips"] = fixed_ips
    return requests.post(url=url, headers=headers, data=json.dumps({"port": port}))


# ==========================================================================
# Slice-specific
# ==========================================================================
def _check(r, expected, op):
    """Prints status and body to stderr if response is undesired."""
    if r.status_code != expected:
        print(f"[{op}] HTTP {r.status_code}: {r.text}", file=sys.stderr)
        return False
    return True

def sf_get_admin_token():
    r = password_authentication_with_scoped_authorization(
        KEYSTONE_ENDPOINT, ADMIN_USER_ID, ADMIN_USER_PASSWORD, DOMAIN_ID, ADMIN_PROJECT_ID)
    return r.headers["X-Subject-Token"] if _check(r, 201, "get_admin_token") else ""

def sf_get_token_for_project(project_id, admin_token):
    r = token_authentication_with_scoped_authorization(
        KEYSTONE_ENDPOINT, admin_token, DOMAIN_ID, project_id)
    return r.headers["X-Subject-Token"] if _check(r, 201, "get_token_for_project") else ""

def sf_create_project(admin_token, name, description=""):
    r = create_project(KEYSTONE_ENDPOINT, admin_token, DOMAIN_ID, name, description)
    return r.json()["project"]["id"] if _check(r, 201, "create_project") else ""

def sf_create_user(admin_token, user_name, password, default_project_id=None):
    r = create_user(KEYSTONE_ENDPOINT, admin_token, DOMAIN_ID,
                    user_name, password, default_project_id=default_project_id)
    return r.json()["user"]["id"] if _check(r, 201, "create_user") else ""

def sf_assign_role(admin_token, project_id, user_id, role_id):
    r = assign_role_to_user_on_project(KEYSTONE_ENDPOINT, admin_token, project_id, user_id, role_id)
    return 1 if _check(r, 204, "assign_role") else 0

def sf_create_network(token, name, shared=False, provider_type=None,
                      provider_physical_network=None, router_external=False,
                      port_security_enabled=False):
    r = create_network(NEUTRON_ENDPOINT, token, name,
                       shared=shared,
                       provider_network_type=provider_type,
                       provider_physical_network=provider_physical_network,
                       router_external=router_external,
                       port_security_enabled=port_security_enabled)
    return r.json()["network"]["id"] if _check(r, 201, "create_network") else ""

def sf_create_subnet(token, name, network_id, cidr, ip_version=4,
                     enable_dhcp=False, gateway_ip=None,
                     allocation_pools=None, dns_nameservers=None):
    r = create_subnet(NEUTRON_ENDPOINT, token, network_id, name, ip_version, cidr,
                      enable_dhcp=enable_dhcp, gateway_ip=gateway_ip,
                      allocation_pools=allocation_pools, dns_nameservers=dns_nameservers)
    return r.json()["subnet"]["id"] if _check(r, 201, "create_subnet") else ""

def sf_create_port(token, name, network_id, project_id, fixed_ips=None, port_security_enabled=False):
    r = create_port(NEUTRON_ENDPOINT, token, name, network_id, project_id,
                    fixed_ips=fixed_ips, port_security_enabled=port_security_enabled)
    return r.json()["port"]["id"] if _check(r, 201, "create_port") else ""

def sf_create_instance(token, image_id, flavor_id, name, port_list):
    ports = [{"port": pid} for pid in port_list]
    r = create_server(NOVA_ENDPOINT, token, name, flavor_id, image_id, ports)
    return r.json() if _check(r, 202, "create_instance") else {}

def sf_get_console_url(admin_token, instance_id):
    r = get_server_console(NOVA_ENDPOINT, admin_token, instance_id, COMPUTE_API_VERSION)
    return r.json()["remote_console"]["url"] if _check(r, 200, "get_console_url") else ""


# ==========================================================================
# Helpers added for the 3-node ring (image/flavor lookup, waiting for ACTIVE)
# ==========================================================================
def sf_find_image_id(token, name):
    """GET /v2/images?name=<name> (Glance). Returns the ID of the first match."""
    r = requests.get(GLANCE_ENDPOINT + "/images", params={"name": name},
                     headers={"X-Auth-Token": token})
    if not _check(r, 200, "find_image"):
        return ""
    images = r.json().get("images", [])
    return images[0]["id"] if images else ""

def sf_find_flavor_id(token, name):
    """GET /flavors/detail (Nova). Returns the ID of the flavor with that name."""
    r = requests.get(NOVA_ENDPOINT + "/flavors/detail", params={"is_public": "None"},
                     headers={"X-Auth-Token": token})
    if not _check(r, 200, "find_flavor"):
        return ""
    for f in r.json().get("flavors", []):
        if f["name"] == name:
            return f["id"]
    return ""

def list_servers_by_project(nova_endpoint, token, project_id):
    url = f"{nova_endpoint}/servers/detail?all_tenants=True&project_id={project_id}"
    r = requests.get(url, headers={"X-Auth-Token": token})
    return r.json()["servers"] if r.status_code == 200 else []

def sf_wait_active(token, project_id, names, timeout=300):
    """Waits until every instance in `names` is ACTIVE (or ERROR)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        servers = {s["name"]: s["status"] for s in
                   list_servers_by_project(NOVA_ENDPOINT, token, project_id)}
        if all(servers.get(n) in ("ACTIVE", "ERROR") for n in names):
            return servers
        time.sleep(5)
    return servers


# ============================== Slice parameters ===================================
# EDIT HERE: values for your group
X                 = os.getenv("SUBNET_X", "110")          # 10.60.X.0/24 (Grupo 2: 110)
NEW_USER_NAME     = os.getenv("NEW_USER_NAME", "userG002")
NEW_USER_PASSWORD = os.getenv("SLICE_USER_PASSWORD") or os.getenv("NEW_USER_PASSWORD", "")
IMAGE_NAME        = os.getenv("IMAGE_NAME", "cirros")
FLAVOR_NAME       = os.getenv("FLAVOR_NAME", "")         # empty = first flavor available
SLICE_NAME        = "topo1_lab6"
SLICE_DESCRIPTION = "Anillo de 3 nodos (Lab 6)"

if not NEW_USER_PASSWORD:
    raise SystemExit("Define SLICE_USER_PASSWORD (en el .env o como variable de entorno).")

# Ring: instance_1 -- link1 -- instance_2 -- link2 -- instance_3 -- link3 -- instance_1
LINKS = {
    "link1": {"cidr": "192.168.1.0/30", "ends": {"instance_1": "192.168.1.1", "instance_2": "192.168.1.2"}},
    "link2": {"cidr": "192.168.2.0/30", "ends": {"instance_2": "192.168.2.1", "instance_3": "192.168.2.2"}},
    "link3": {"cidr": "192.168.3.0/30", "ends": {"instance_3": "192.168.3.1", "instance_1": "192.168.3.2"}},
}
# Instances with Internet access (port on the provider network "external")
EXTERNAL_IPS = {"instance_1": f"10.60.{X}.21", "instance_2": f"10.60.{X}.22"}
# Order of the ports = order of the interfaces inside the VM (eth0, eth1, eth2)
INSTANCES = {
    "instance_1": ["port1_ext", "port1_link1", "port1_link3"],
    "instance_2": ["port2_ext", "port2_link1", "port2_link2"],
    "instance_3": ["port3_link2", "port3_link3"],
}


# ============================== Slice creation steps ===================================

# Step 1: Admin token (Keystone)
admin_token = sf_get_admin_token()
if not admin_token:
    raise SystemExit("No se obtuvo el token admin. Revisa el .env y los túneles SSH.")
print("admin_token:", admin_token[:24] + "...")

# Step 2: Project (reused if it already exists from a previous run)
def _find_in_domain(kind, name):
    r = requests.get(f"{KEYSTONE_ENDPOINT}/{kind}", params={"name": name, "domain_id": DOMAIN_ID},
                     headers={"X-Auth-Token": admin_token})
    items = r.json().get(kind, []) if r.status_code == 200 else []
    return items[0]["id"] if items else ""

project_id = _find_in_domain("projects", SLICE_NAME)
if project_id:
    print("Proyecto ya existente, se reutiliza.")
else:
    project_id = sf_create_project(admin_token, SLICE_NAME, SLICE_DESCRIPTION)
if not project_id:
    raise SystemExit("No se pudo crear el proyecto (¿ya existe? corre primero la limpieza).")
print("project_id:", project_id)

# Step 3: Project owner user
new_user_id = _find_in_domain("users", NEW_USER_NAME)
if new_user_id:
    print("Usuario ya existente, se reutiliza.")
else:
    new_user_id = sf_create_user(admin_token, NEW_USER_NAME, NEW_USER_PASSWORD, default_project_id=project_id)
print("new_user_id:", new_user_id or "(user was not created)")

# Step 4: Roles (admin user, and project owner user)
print("admin_user -> status:", sf_assign_role(admin_token, project_id, ADMIN_USER_ID, ADMIN_ROLE_ID))
if new_user_id:
    print("new_user   -> status:", sf_assign_role(admin_token, project_id, new_user_id, ADMIN_ROLE_ID))

# Step 5: Project-scoped token
scoped_token = sf_get_token_for_project(project_id, admin_token)
if not scoped_token:
    raise SystemExit("Could not get the project-scoped token.")
print("scoped_token:", scoped_token[:24] + "...")

# Step 6: Networks — provider "external" (flat, physnet0) + one network per ring link
networks_created = {}
nid = sf_create_network(admin_token, "external", shared=True, provider_type="flat",
                        provider_physical_network="physnet0", router_external=True)
if not nid:
    raise SystemExit("Could not create the 'external' network.")
networks_created["external"] = nid
for link in LINKS:
    nid = sf_create_network(scoped_token, link)
    if not nid:
        raise SystemExit(f"Could not create network {link}.")
    networks_created[link] = nid
print("networks_created:", networks_created)

# Step 7: Subnets — external with DHCP and gateway; links without gateway or DHCP
subnets_created = {}
sid = sf_create_subnet(admin_token, "external_subnet", networks_created["external"],
                       f"10.60.{X}.0/24", enable_dhcp=True, gateway_ip=f"10.60.{X}.1",
                       allocation_pools=[{"start": f"10.60.{X}.10", "end": f"10.60.{X}.250"}],
                       dns_nameservers=["8.8.8.8"])
if not sid:
    raise SystemExit("Could not create external_subnet.")
subnets_created["external"] = sid
for link, cfg in LINKS.items():
    sid = sf_create_subnet(scoped_token, f"{link}_subnet", networks_created[link],
                           cfg["cidr"], enable_dhcp=False, gateway_ip=None)
    if not sid:
        raise SystemExit(f"Could not create {link}_subnet.")
    subnets_created[link] = sid
print("subnets_created:", subnets_created)

# Step 8: Ports (port security disabled so that ping works between VMs)
ports_created = {}
def _new_port(name, net, ip):
    pid = sf_create_port(admin_token, name, networks_created[net], project_id,
                         fixed_ips=[{"ip_address": ip, "subnet_id": subnets_created[net]}])
    if not pid:
        raise SystemExit(f"Could not create port {name}.")
    ports_created[name] = pid

for inst, ip in EXTERNAL_IPS.items():
    _new_port(f"port{inst[-1]}_ext", "external", ip)
for link, cfg in LINKS.items():
    for inst, ip in cfg["ends"].items():
        _new_port(f"port{inst[-1]}_{link}", link, ip)
print("ports_created:", ports_created)

# Step 9: Instances
image_id = os.getenv("IMAGE_ID") or sf_find_image_id(admin_token, IMAGE_NAME)
if FLAVOR_NAME:
    flavor_id = os.getenv("FLAVOR_ID") or sf_find_flavor_id(admin_token, FLAVOR_NAME)
else:
    r = requests.get(NOVA_ENDPOINT + "/flavors", headers={"X-Auth-Token": admin_token})
    flavors = r.json().get("flavors", []) if r.status_code == 200 else []
    flavor_id = os.getenv("FLAVOR_ID") or (flavors[0]["id"] if flavors else "")
if not image_id or not flavor_id:
    raise SystemExit(f"Image ({image_id!r}) or flavor ({flavor_id!r}) not found.")
print("image_id:", image_id, "| flavor_id:", flavor_id)

instances_created = {}
for inst, port_names in INSTANCES.items():
    info = sf_create_instance(scoped_token, image_id, flavor_id, inst,
                              [ports_created[n] for n in port_names])
    iid = info.get("server", {}).get("id", "")
    if iid:
        instances_created[inst] = iid
print("instances_created:", instances_created)

# Verification (lab step 5): instance status
print("Esperando a que las instancias estén ACTIVE...")
sf_wait_active(admin_token, project_id, list(instances_created))
for s in list_servers_by_project(NOVA_ENDPOINT, admin_token, project_id):
    print(" ", s["id"], s["name"], s["status"])

# Step 10: noVNC console of each instance
consoles = {inst: sf_get_console_url(admin_token, iid) for inst, iid in instances_created.items()}


# ==========================================================================
# Summary
# ==========================================================================
print("=" * 70)
print(f"Slice            : {SLICE_NAME}  ->  {project_id}")
print(f"Usuario nuevo    : {new_user_id or '(no se creó)'}")
print("-" * 70)
for title, d in (("Redes creadas", networks_created), ("Subredes creadas", subnets_created),
                 ("Puertos creados", ports_created), ("Instancias creadas", instances_created)):
    print(f"{title}:")
    for n, i in d.items():
        print(f"  {n:25s} {i}")
print("Consolas noVNC (abrir con el túnel del puerto 56080):")
for inst, url in consoles.items():
    print(f"  {inst}: {re.sub(r'//[^/]+', '//localhost:56080', url, count=1) if url else '(no disponible)'}")
print("=" * 70)