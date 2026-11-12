"""
driver_openstack.py
-------------------
Driver OpenStack (R3): despliega los slices del orquestador sobre un clúster
OpenStack usando únicamente sus APIs REST (Keystone, Neutron y Nova), el mismo
flujo del Lab 6 (``lab6.py``) pero empaquetado como un driver.

Tiene los MISMOS métodos y firmas que ``DriverLinux`` (crear_red, crear_vm,
eliminar_vm, eliminar_red, asegurar_imagen, liberar_imagen_si_huerfana), de modo
que ``orchestrator.desplegar_slice_existente`` lo usa sin cambios. Lo único
distinto es que OpenStack tiene el concepto de *proyecto*: el orquestador llama
(si existen) a ``preparar_slice(slc)`` antes de crear recursos y a
``liberar_slice(slc.id)`` al terminar o revertir.

Correspondencia con el diseño (DriverManager -> driver):

    crear_red(...)       Neutron: POST /networks y POST /subnets  (una por enlace)
    crear_vm(...)        Neutron: POST /ports (uno por interfaz, sin port security)
                         Nova:    POST /servers  (con la lista de puertos)
    eliminar_vm(...)     Nova: DELETE /servers/{id}; luego Neutron: DELETE /ports/{id}
    eliminar_red(...)    Neutron: DELETE /subnets/{id} y DELETE /networks/{id}
    preparar_slice(...)  Keystone: POST /projects y PUT .../roles/{rol}
    liberar_slice(...)   Keystone: DELETE /projects/{id}
    descubrir_hosts()    Nova: GET /os-hypervisors/detail y /os-availability-zone/detail

Ubicación (``host_uri``): para OpenStack es ``openstack://<zona>`` o
``openstack://<zona>:<host>``. La zona de disponibilidad es el mecanismo de
enforcement; con ``<zona>:<host>`` el administrador fuerza el worker concreto
(``availability_zone = "zona:host"`` de Nova), que es lo que decide VM Placement.

Modos:
    SIMULADO  (predeterminado) no abre ninguna conexión: usa ``SesionSimulada``,
              un OpenStack en memoria que además guarda cada petición que el
              driver enviaría (``sesion.comandos``). Sirve para pruebas unitarias.
    REAL      envía las peticiones HTTP al clúster (requiere ``requests``).

ESTADO DE VALIDACIÓN: el flujo de peticiones sigue el de ``lab6.py`` (que sí se
ejecutó en el clúster), pero este driver todavía NO se probó de punta a punta
contra el clúster real. Las pruebas actuales usan el modo simulado.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .models import CATALOGO_FLAVORS, Host

logger = logging.getLogger(__name__)

PREFIJO_UBICACION = "openstack://"


class ModoOpenStack(str, Enum):
    SIMULADO = "simulado"
    REAL = "real"


class DriverOpenStackError(Exception):
    """Error al hablar con OpenStack (respuesta inesperada, recurso faltante...)."""


# ---------------------------------------------------------------- configuración
@dataclass
class ConfigOpenStack:
    """Datos de acceso al clúster. Los valores reales NO van en el código:
    se leen del entorno / archivo .env (mismos nombres que usa lab6.py)."""

    acceso_ip: str = "localhost"
    puerto_keystone: int = 55000
    puerto_nova: int = 58774
    puerto_neutron: int = 59696
    puerto_glance: int = 9292
    dominio_id: str = ""
    proyecto_admin_id: str = ""
    usuario_admin_id: str = ""
    password_admin: str = field(default="", repr=False)
    rol_admin_id: str = ""
    version_compute: str = "2.87"

    @property
    def keystone(self) -> str:
        return f"http://{self.acceso_ip}:{self.puerto_keystone}/v3"

    @property
    def nova(self) -> str:
        return f"http://{self.acceso_ip}:{self.puerto_nova}/v2.1"

    @property
    def neutron(self) -> str:
        return f"http://{self.acceso_ip}:{self.puerto_neutron}/v2.0"

    @property
    def glance(self) -> str:
        return f"http://{self.acceso_ip}:{self.puerto_glance}/v2"

    @classmethod
    def desde_entorno(cls, env: dict | None = None) -> "ConfigOpenStack":
        e = os.environ if env is None else env
        return cls(
            acceso_ip=e.get("ACCESS_NODE_IP", "localhost"),
            puerto_keystone=int(e.get("KEYSTONE_PORT", 55000)),
            puerto_nova=int(e.get("NOVA_PORT", 58774)),
            puerto_neutron=int(e.get("NEUTRON_PORT", 59696)),
            puerto_glance=int(e.get("GLANCE_PORT", 9292)),
            dominio_id=e.get("DOMAIN_ID", ""),
            proyecto_admin_id=e.get("ADMIN_PROJECT_ID", ""),
            usuario_admin_id=e.get("ADMIN_USER_ID", ""),
            password_admin=e.get("ADMIN_USER_PASSWORD", ""),
            rol_admin_id=e.get("ADMIN_ROLE_ID", ""),
            version_compute=e.get("COMPUTE_API_VERSION", "2.87"),
        )

    def faltantes(self) -> list[str]:
        campos = {"DOMAIN_ID": self.dominio_id, "ADMIN_PROJECT_ID": self.proyecto_admin_id,
                  "ADMIN_USER_ID": self.usuario_admin_id,
                  "ADMIN_USER_PASSWORD": self.password_admin,
                  "ADMIN_ROLE_ID": self.rol_admin_id}
        return [k for k, v in campos.items() if not v]


def ubicacion(zona: str, host: str | None = None) -> str:
    """Construye el ``host_uri`` de un host OpenStack."""
    return f"{PREFIJO_UBICACION}{zona}" + (f":{host}" if host else "")


def parsear_ubicacion(host_uri: str) -> tuple[str, str | None]:
    """'openstack://az1:worker2' -> ('az1', 'worker2'); 'openstack://az1' -> ('az1', None)."""
    resto = host_uri[len(PREFIJO_UBICACION):] if host_uri.startswith(PREFIJO_UBICACION) else host_uri
    zona, _, host = resto.partition(":")
    return zona, (host or None)


def _normalizar_nombre_imagen(nombre: str) -> str:
    """'cirros-0.6.2.qcow2' -> 'cirros'; 'ubuntu-22.04.qcow2' -> 'ubuntu'.

    Quita la extensión del archivo y se queda con las letras iniciales, para
    poder emparejar el nombre de archivo del catálogo (`models.Flavor.imagen`)
    con el nombre, normalmente más corto, con el que un administrador subió la
    imagen a Glance (ej. solo 'cirros' en vez de 'cirros-0.6.2.qcow2')."""
    base = nombre.rsplit(".", 1)[0]
    m = re.match(r"[a-zA-Z]+", base)
    return (m.group(0) if m else base).lower()


# ----------------------------------------------------------------------- driver
@dataclass
class DriverOpenStack:
    modo: ModoOpenStack = ModoOpenStack.SIMULADO
    config: ConfigOpenStack = field(default_factory=ConfigOpenStack)
    sesion: Any = None                       # objeto con .request(...) (requests.Session o SesionSimulada)
    proyecto_id: str | None = None           # proyecto fijo (si no se usa preparar_slice)
    red_externa: str = "external"            # red provider flat existente (salida a Internet)
    imagen_por_nombre: dict[str, str] = field(default_factory=dict)   # nombre en el catálogo -> id en Glance
    imagen_id_defecto: str | None = None
    flavor_id_defecto: str | None = None
    flavor_por_perfil: dict[tuple[int, int, float], str] = field(default_factory=dict)  # (vcpu, ram, disco)->id
    cidr_por_red: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 30.0
    reintentos_conexion: int = 3
    espera_reintento_s: float = 2.0
    espera_borrado_s: float = 60.0
    intervalo_borrado_s: float = 2.0
    historial: list[tuple[str, str, str]] = field(default_factory=list)
    fallar_en_operacion: int | None = None
    fallas_consecutivas: int = 1
    _numero_operacion: int = 0
    _token_admin: str | None = field(default=None, repr=False)
    _token_proyecto: str | None = field(default=None, repr=False)
    _proyecto_creado: bool = False
    _redes: dict[str, str] = field(default_factory=dict)
    _subredes: dict[str, str] = field(default_factory=dict)
    _ips_puerto: dict[tuple[str, str], str] = field(default_factory=dict)

    # ------------------------------------------------------------ utilidades
    def _registrar(self, accion: str, host_uri: str, recurso: str) -> None:
        """Igual que en DriverLinux: cuenta operaciones, permite inyectar fallas
        (simulación de errores para probar reintentos/rollback) y deja historial."""
        self._numero_operacion += 1
        n = self._numero_operacion
        if (self.fallar_en_operacion is not None
                and self.fallar_en_operacion <= n < self.fallar_en_operacion + self.fallas_consecutivas):
            raise DriverOpenStackError(f"Falla simulada en {accion} de {recurso}")
        self.historial.append((accion, host_uri, recurso))

    def _http(self):
        if self.sesion is None:
            if self.modo == ModoOpenStack.SIMULADO:
                self.sesion = SesionSimulada(self.config)
            else:
                import requests            # import perezoso: solo el modo real lo necesita
                self.sesion = requests.Session()
        return self.sesion

    def _enviar(self, metodo: str, url: str, token: str | None = None, cuerpo: dict | None = None,
                params: dict | None = None, esperado: tuple[int, ...] = (200,),
                tolerar: tuple[int, ...] = (), extra: dict | None = None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Auth-Token"] = token
        if extra:
            headers.update(extra)
        ultimo: Exception | None = None
        for intento in range(1, max(1, self.reintentos_conexion) + 1):
            try:
                r = self._http().request(metodo, url, headers=headers, json=cuerpo,
                                         params=params, timeout=self.timeout_s)
                break
            except (ConnectionError, TimeoutError, OSError) as exc:
                ultimo = exc
                if intento >= self.reintentos_conexion:
                    raise DriverOpenStackError(f"No se pudo conectar a {url}: {exc}") from exc
                logger.warning("Conexión a %s falló (intento %d/%d): %s",
                               url, intento, self.reintentos_conexion, exc)
                time.sleep(self.espera_reintento_s)
        else:                                     # pragma: no cover
            raise DriverOpenStackError(f"No se pudo conectar a {url}: {ultimo}")
        if r.status_code not in esperado and r.status_code not in tolerar:
            raise DriverOpenStackError(
                f"{metodo} {url} -> HTTP {r.status_code} (esperado {esperado}): {str(r.text)[:300]}")
        return r

    # ------------------------------------------------------------- autenticación
    def _autenticar(self, alcance_proyecto: str, token_previo: str | None = None) -> str:
        c = self.config
        if token_previo:
            identidad = {"methods": ["token"], "token": {"id": token_previo}}
        else:
            identidad = {"methods": ["password"],
                         "password": {"user": {"id": c.usuario_admin_id,
                                               "domain": {"id": c.dominio_id},
                                               "password": c.password_admin}}}
        cuerpo = {"auth": {"identity": identidad,
                           "scope": {"project": {"domain": {"id": c.dominio_id}, "id": alcance_proyecto}}}}
        r = self._enviar("POST", c.keystone + "/auth/tokens", cuerpo=cuerpo, esperado=(201,))
        token = r.headers.get("X-Subject-Token")
        if not token:
            raise DriverOpenStackError("Keystone no devolvió X-Subject-Token")
        return token

    def _admin(self) -> str:
        if self._token_admin is None:
            faltan = self.config.faltantes() if self.modo == ModoOpenStack.REAL else []
            if faltan:
                raise DriverOpenStackError("Faltan credenciales: " + ", ".join(faltan))
            self._token_admin = self._autenticar(self.config.proyecto_admin_id)
        return self._token_admin

    def _proyecto(self) -> str:
        if self.proyecto_id is None:
            raise DriverOpenStackError(
                "No hay proyecto activo: llame a preparar_slice() o defina proyecto_id")
        if self._token_proyecto is None:
            self._token_proyecto = self._autenticar(self.proyecto_id, token_previo=self._admin())
        return self._token_proyecto

    def _api(self, tipo: str, metodo: str, url: str, **kw):
        """Llama con el token ``tipo`` ('admin' | 'proyecto'); si Keystone responde 401
        (token vencido) re-autentica una vez y repite."""
        for _ in (1, 2):
            token = self._admin() if tipo == "admin" else self._proyecto()
            r = self._enviar_con_401(metodo, url, token, kw)
            if r.status_code != 401:
                return r
            self._token_admin = None
            self._token_proyecto = None
        raise DriverOpenStackError(f"{metodo} {url}: la autenticación fue rechazada (401)")

    def _enviar_con_401(self, metodo, url, token, kw):
        kw = dict(kw)
        kw["tolerar"] = tuple(kw.get("tolerar", ())) + (401,)
        return self._enviar(metodo, url, token=token, **kw)

    # ------------------------------------------------------------------ proyecto
    def preparar_slice(self, slc) -> str:
        """Crea (o reutiliza) el proyecto del slice, asigna el rol de administrador
        y calcula, desde ``direccionamiento``, la subred y la IP de cada interfaz
        para que coincidan con lo que configura cloud-init."""
        from . import direccionamiento
        c = self.config
        nombre = f"orq_{slc.id}"
        r = self._api("admin", "GET", c.keystone + "/projects", params={"name": nombre})
        existentes = r.json().get("projects", [])
        if existentes:
            self.proyecto_id = existentes[0]["id"]
            self._proyecto_creado = False
        else:
            r = self._api("admin", "POST", c.keystone + "/projects", esperado=(201,),
                          cuerpo={"project": {"name": nombre, "description": f"slice {slc.id}",
                                              "domain_id": c.dominio_id}})
            self.proyecto_id = r.json()["project"]["id"]
            self._proyecto_creado = True
        self._token_proyecto = None
        self._api("admin", "PUT",
                  f"{c.keystone}/projects/{self.proyecto_id}/users/{c.usuario_admin_id}/roles/{c.rol_admin_id}",
                  esperado=(204,))
        for i, e in enumerate(slc.enlaces):
            if e.nombre_red:
                self.cidr_por_red[e.nombre_red] = str(direccionamiento.subred_del_enlace(i))
        for n in slc.nodos:
            for itf in direccionamiento.interfaces_de(slc, n):
                self._ips_puerto[(n.nombre, itf.red)] = itf.cidr.split("/")[0]
        logger.info("[OPENSTACK] proyecto %s listo (%s)", nombre, self.proyecto_id)
        return self.proyecto_id

    def liberar_slice(self, slice_id: str) -> None:
        """Elimina el proyecto si lo creó este driver (no toca proyectos ajenos)."""
        if self.proyecto_id and self._proyecto_creado:
            self._api("admin", "DELETE", f"{self.config.keystone}/projects/{self.proyecto_id}",
                      esperado=(204,), tolerar=(404,))
            logger.info("[OPENSTACK] proyecto %s eliminado", self.proyecto_id)
        self.proyecto_id = None
        self._proyecto_creado = False
        self._token_proyecto = None
        self._redes.clear()
        self._subredes.clear()
        self._ips_puerto.clear()

    # --------------------------------------------------------------------- redes
    def _buscar(self, coleccion: str, nombre: str, clave: str) -> str | None:
        r = self._api("admin", "GET", f"{self.config.neutron}/{coleccion}",
                      params={"name": nombre, "project_id": self.proyecto_id})
        items = r.json().get(clave, [])
        return items[0]["id"] if items else None

    def _cidr_de(self, nombre_red: str, vlan_id: int) -> str:
        if nombre_red in self.cidr_por_red:
            return self.cidr_por_red[nombre_red]
        if vlan_id is None or vlan_id < 0 or vlan_id * 4 >= 65536:
            raise DriverOpenStackError(f"No se puede derivar un CIDR para {nombre_red} (vlan={vlan_id})")
        desplazamiento = vlan_id * 4
        return f"10.30.{(desplazamiento // 256) % 256}.{desplazamiento % 256}/30"

    def crear_red(self, host_uri: str, nombre_red: str, vlan_id: int) -> str:
        """Crea la red del enlace y su subred /30. Es idempotente y global al
        proyecto: el orquestador la pide una vez por host extremo, aquí solo se
        crea la primera vez. La VLAN real la asigna Neutron (tenant network)."""
        self._registrar("crear_red", host_uri, nombre_red)
        self._proyecto()
        if nombre_red in self._redes:
            return self._redes[nombre_red]
        c = self.config
        red_id = self._buscar("networks", nombre_red, "networks")
        if red_id is None:
            r = self._api("admin", "POST", c.neutron + "/networks", esperado=(201,),
                          cuerpo={"network": {"name": nombre_red, "project_id": self.proyecto_id,
                                              "port_security_enabled": False}})
            red_id = r.json()["network"]["id"]
        sub_nombre = f"{nombre_red}_subnet"
        sub_id = self._buscar("subnets", sub_nombre, "subnets")
        if sub_id is None:
            r = self._api("admin", "POST", c.neutron + "/subnets", esperado=(201,),
                          cuerpo={"subnet": {"network_id": red_id, "name": sub_nombre,
                                             "project_id": self.proyecto_id, "ip_version": 4,
                                             "cidr": self._cidr_de(nombre_red, vlan_id),
                                             "enable_dhcp": False, "gateway_ip": None}})
            sub_id = r.json()["subnet"]["id"]
        self._redes[nombre_red] = red_id
        self._subredes[nombre_red] = sub_id
        logger.info("[OPENSTACK] red %s (%s) con subred %s", nombre_red, red_id, sub_id)
        return red_id

    def eliminar_red(self, host_uri: str, nombre_red: str) -> None:
        """Borra subred y red. Idempotente (404 = ya no existe)."""
        self._registrar("eliminar_red", host_uri, nombre_red)
        if self.proyecto_id is None:
            return
        c = self.config
        sub_id = self._subredes.pop(nombre_red, None) or self._buscar("subnets", f"{nombre_red}_subnet", "subnets")
        red_id = self._redes.pop(nombre_red, None) or self._buscar("networks", nombre_red, "networks")
        if sub_id:
            self._api("admin", "DELETE", f"{c.neutron}/subnets/{sub_id}", esperado=(204,), tolerar=(404,))
        if red_id:
            self._api("admin", "DELETE", f"{c.neutron}/networks/{red_id}", esperado=(204,), tolerar=(404,))

    # ------------------------------------------------------------------- imágenes
    def asegurar_imagen(self, host_uri: str, imagen: str) -> bool:
        """En OpenStack las imágenes viven en Glance y son globales: no se copia
        nada a los workers. Comprueba que la imagen esté mapeada y devuelve False
        (nada que deshacer en el rollback)."""
        self._registrar("asegurar_imagen", host_uri, imagen)
        self._resolver_imagen(imagen)
        return False

    def liberar_imagen_si_huerfana(self, host_uri: str, imagen: str) -> bool:
        self._registrar("liberar_imagen", host_uri, imagen)
        return False

    def sincronizar_catalogo_imagenes(self, forzar: bool = False) -> dict[str, str]:
        """Resuelve automáticamente `imagen_por_nombre` consultando Glance, para
        que el usuario final no tenga que conocer de antemano los IDs: por cada
        imagen del catálogo (``models.CATALOGO_FLAVORS``) busca en Glance una
        imagen cuyo nombre coincida (normalizado, sin extensión ni versión; ej.
        'cirros-0.6.2.qcow2' empareja con la imagen de Glance llamada 'cirros').

        Un mapeo puesto a mano en `imagen_por_nombre` al construir el driver NO
        se pisa (tiene prioridad): esto solo RELLENA lo que falte. Si ninguna
        imagen del catálogo encuentra pareja, usa la primera imagen disponible
        de Glance como `imagen_id_defecto`, para no bloquear la demo.
        """
        if self.imagen_por_nombre and not forzar:
            return self.imagen_por_nombre
        r = self._api("admin", "GET", self.config.glance + "/images")
        disponibles = {img["name"]: img["id"] for img in r.json().get("images", [])}
        disponibles_norm = {_normalizar_nombre_imagen(n): id_ for n, id_ in disponibles.items()}
        for flavor in CATALOGO_FLAVORS.values():
            clave = _normalizar_nombre_imagen(flavor.imagen)
            if clave in disponibles_norm:
                self.imagen_por_nombre[flavor.imagen] = disponibles_norm[clave]
        if self.imagen_id_defecto is None and disponibles:
            self.imagen_id_defecto = next(iter(disponibles.values()))
        logger.info("[OPENSTACK] catálogo de imágenes sincronizado desde Glance: %s "
                    "(defecto=%s)", self.imagen_por_nombre, self.imagen_id_defecto)
        return self.imagen_por_nombre

    def _resolver_imagen(self, imagen: str) -> str:
        id_ = self.imagen_por_nombre.get(imagen)
        if not id_ and self.modo == ModoOpenStack.REAL:
            # Nadie configuró un mapeo manual: lo intentamos resolver solos
            # contra Glance antes de rendirnos (automatización end-to-end).
            self.sincronizar_catalogo_imagenes()
            id_ = self.imagen_por_nombre.get(imagen)
        id_ = id_ or self.imagen_id_defecto
        if not id_:
            raise DriverOpenStackError(f"No hay id de Glance para la imagen '{imagen}'")
        return id_

    def sincronizar_catalogo_flavors(self, forzar: bool = False) -> dict[tuple[int, int, float], str]:
        """Resuelve automáticamente `flavor_por_perfil` consultando Nova, para
        que el usuario final no tenga que conocer de antemano los IDs de
        flavor: por cada flavor del catálogo (``models.CATALOGO_FLAVORS``)
        busca en Nova uno con las mismas vCPU y RAM, y al menos el disco
        pedido (Nova no deja usar un flavor con menos disco del que la imagen
        necesita).

        Un mapeo puesto a mano en `flavor_por_perfil` al construir el driver
        NO se pisa (tiene prioridad): esto solo RELLENA lo que falte. Si
        ningún flavor del catálogo encuentra pareja, usa el primer flavor
        disponible de Nova como `flavor_id_defecto`, para no bloquear la demo.
        """
        if self.flavor_por_perfil and not forzar:
            return self.flavor_por_perfil
        r = self._api("admin", "GET", self.config.nova + "/flavors/detail")
        disponibles = r.json().get("flavors", [])
        for flavor in CATALOGO_FLAVORS.values():
            clave = (flavor.vcpu, flavor.ram_mb, flavor.disco_gb)
            if clave in self.flavor_por_perfil:
                continue
            candidato = next(
                (f for f in disponibles
                 if f["vcpus"] == flavor.vcpu and f["ram"] == flavor.ram_mb
                 and f["disk"] >= flavor.disco_gb),
                None,
            )
            if candidato:
                self.flavor_por_perfil[clave] = candidato["id"]
        if self.flavor_id_defecto is None and disponibles:
            self.flavor_id_defecto = disponibles[0]["id"]
        logger.info("[OPENSTACK] catálogo de flavors sincronizado desde Nova: %s "
                    "(defecto=%s)", self.flavor_por_perfil, self.flavor_id_defecto)
        return self.flavor_por_perfil

    def _resolver_flavor(self, vcpu: int, ram_mb: int, disco_gb: float) -> str:
        clave = (vcpu, ram_mb, disco_gb)
        id_ = self.flavor_por_perfil.get(clave)
        if not id_ and self.modo == ModoOpenStack.REAL:
            # Nadie configuró un mapeo manual: lo intentamos resolver solos
            # contra Nova antes de rendirnos (automatización end-to-end).
            self.sincronizar_catalogo_flavors()
            id_ = self.flavor_por_perfil.get(clave)
        id_ = id_ or self.flavor_id_defecto
        if not id_:
            raise DriverOpenStackError(
                f"No hay flavor de Nova para {vcpu} vCPU / {ram_mb} MB / {disco_gb} GB")
        return id_

    # ------------------------------------------------------------------------ VMs
    def _nombre_puerto(self, nombre_vm: str, destino: str) -> str:
        return f"{nombre_vm}:{destino}"

    def _crear_puerto(self, nombre: str, red_id: str, mac: str | None, ip: str | None,
                      subred_id: str | None) -> str:
        existente = self._buscar("ports", nombre, "ports")
        if existente:
            return existente
        puerto: dict[str, Any] = {"name": nombre, "network_id": red_id, "project_id": self.proyecto_id,
                                  "tenant_id": self.proyecto_id, "port_security_enabled": False}
        if mac:
            puerto["mac_address"] = mac
        if ip and subred_id:
            puerto["fixed_ips"] = [{"subnet_id": subred_id, "ip_address": ip}]
        r = self._api("admin", "POST", self.config.neutron + "/ports", esperado=(201,),
                      cuerpo={"port": puerto})
        return r.json()["port"]["id"]

    def crear_vm(self, host_uri: str, nombre_vm: str, vcpu: int, ram_mb: int,
                 disco_gb: float, imagen: str, redes: list[tuple[str, int]],
                 macs: list[str] | None = None,
                 semilla: dict[str, str] | None = None,
                 acceso: dict | None = None) -> str:
        """Crea un puerto por cada red del enlace (con la MAC y la IP que usa
        cloud-init), un puerto extra en la red externa si ``acceso`` lo pide, y la
        instancia en la zona (o worker) indicada por ``host_uri``. Idempotente."""
        self._registrar("crear_vm", host_uri, nombre_vm)
        self._proyecto()
        c = self.config
        r = self._api("proyecto", "GET", c.nova + "/servers", params={"name": f"^{nombre_vm}$"})
        existentes = r.json().get("servers", [])
        if existentes:
            return existentes[0]["id"]

        puertos: list[str] = []
        for i, (nombre_red, _vlan) in enumerate(redes):
            red_id = self._redes.get(nombre_red) or self._buscar("networks", nombre_red, "networks")
            if red_id is None:
                raise DriverOpenStackError(f"La red {nombre_red} no existe; ¿se llamó a crear_red?")
            puertos.append(self._crear_puerto(
                self._nombre_puerto(nombre_vm, nombre_red), red_id,
                macs[i] if macs and i < len(macs) else None,
                self._ips_puerto.get((nombre_vm, nombre_red)), self._subredes.get(nombre_red)))
        if acceso is not None:
            ext_id = self._buscar_red_externa()
            puertos.append(self._crear_puerto(self._nombre_puerto(nombre_vm, "acceso"), ext_id,
                                              acceso.get("mac"), None, None))

        zona, host = parsear_ubicacion(host_uri)
        servidor: dict[str, Any] = {
            "name": nombre_vm,
            "flavorRef": self._resolver_flavor(vcpu, ram_mb, disco_gb),
            "imageRef": self._resolver_imagen(imagen),
            "networks": [{"port": p} for p in puertos],
            "availability_zone": f"{zona}:{host}" if host else zona,
        }
        if semilla and semilla.get("user-data"):
            servidor["user_data"] = base64.b64encode(semilla["user-data"].encode()).decode()
            servidor["config_drive"] = True
        r = self._api("proyecto", "POST", c.nova + "/servers", esperado=(202,),
                      cuerpo={"server": servidor})
        return r.json()["server"]["id"]

    def _buscar_red_externa(self) -> str:
        r = self._api("admin", "GET", f"{self.config.neutron}/networks", params={"name": self.red_externa})
        items = r.json().get("networks", [])
        if not items:
            raise DriverOpenStackError(f"No existe la red externa '{self.red_externa}'")
        return items[0]["id"]

    def eliminar_vm(self, host_uri: str, nombre_vm: str) -> None:
        """Borra la instancia, espera a que desaparezca y luego borra sus puertos."""
        self._registrar("eliminar_vm", host_uri, nombre_vm)
        if self.proyecto_id is None:
            return
        c = self.config
        r = self._api("proyecto", "GET", c.nova + "/servers", params={"name": f"^{nombre_vm}$"})
        for s in r.json().get("servers", []):
            self._api("proyecto", "DELETE", f"{c.nova}/servers/{s['id']}", esperado=(204,), tolerar=(404,))
            limite = time.time() + self.espera_borrado_s
            while time.time() < limite:
                g = self._api("proyecto", "GET", f"{c.nova}/servers/{s['id']}", tolerar=(404,))
                if g.status_code == 404:
                    break
                time.sleep(self.intervalo_borrado_s)
            else:
                raise DriverOpenStackError(f"La instancia {nombre_vm} no terminó de borrarse a tiempo")
        r = self._api("admin", "GET", c.neutron + "/ports", params={"project_id": self.proyecto_id})
        for p in r.json().get("ports", []):
            if p.get("name", "").startswith(f"{nombre_vm}:"):
                self._api("admin", "DELETE", f"{c.neutron}/ports/{p['id']}", esperado=(204,), tolerar=(404,))

    # ----------------------------------------------------------------- consulta
    def url_consola(self, nombre_vm: str) -> str:
        """URL noVNC de la instancia (los tokens caducan en pocos minutos)."""
        c = self.config
        r = self._api("proyecto", "GET", c.nova + "/servers", params={"name": f"^{nombre_vm}$"})
        servidores = r.json().get("servers", [])
        if not servidores:
            raise DriverOpenStackError(f"La instancia {nombre_vm} no existe")
        r = self._api("admin", "POST", f"{c.nova}/servers/{servidores[0]['id']}/remote-consoles",
                      cuerpo={"remote_console": {"protocol": "vnc", "type": "novnc"}},
                      extra={"OpenStack-API-Version": "compute " + c.version_compute})
        return r.json()["remote_console"]["url"]

    def descubrir_hosts(self) -> list[Host]:
        """Inventario de los workers desde Nova (para VM Placement y monitoreo).
        Devuelve ``Host`` con ``uri_libvirt = openstack://<zona>:<host>``."""
        c = self.config
        zonas = self._api("admin", "GET", c.nova + "/os-availability-zone/detail").json()
        zona_de: dict[str, str] = {}
        for z in zonas.get("availabilityZoneInfo", []):
            for host, servicios in (z.get("hosts") or {}).items():
                if "nova-compute" in servicios:
                    zona_de[host] = z["zoneName"]
        hip = self._api("admin", "GET", c.nova + "/os-hypervisors/detail").json().get("hypervisors", [])
        hosts = []
        for h in hip:
            nombre = h["hypervisor_hostname"]
            zona = zona_de.get(nombre, "nova")
            hosts.append(Host(
                id=nombre, nombre=nombre, uri_libvirt=ubicacion(zona, nombre),
                capacidad_vcpu=int(h["vcpus"]), capacidad_ram_mb=int(h["memory_mb"]),
                zona_disponibilidad=zona,
                vcpu_usados=int(h.get("vcpus_used", 0)), ram_usados_mb=int(h.get("memory_mb_used", 0))))
        return hosts


# ------------------------------------------------------- OpenStack en memoria
class _Resp:
    def __init__(self, codigo: int, cuerpo: Any = None, headers: dict | None = None):
        self.status_code = codigo
        self._cuerpo = cuerpo
        self.headers = headers or {}
        self.text = "" if cuerpo is None else str(cuerpo)

    def json(self):
        return self._cuerpo


class SesionSimulada:
    """Emula en memoria la parte de Keystone, Neutron y Nova que usa el driver y
    guarda cada petición recibida en ``comandos`` como (método, ruta, cuerpo).
    Permite probar el driver sin clúster y mostrar qué comandos enviaría."""

    def __init__(self, config: ConfigOpenStack | None = None, workers: int = 3):
        self.config = config or ConfigOpenStack()
        self.comandos: list[tuple[str, str, Any]] = []
        self._n = 0
        self.proyectos: dict[str, dict] = {}
        self.redes: dict[str, dict] = {}
        self.subredes: dict[str, dict] = {}
        self.puertos: dict[str, dict] = {}
        self.servidores: dict[str, dict] = {}
        self.roles: set[tuple[str, str, str]] = set()
        self.fallos_http: dict[tuple[str, str], list[int]] = {}   # (método, fragmento de ruta) -> códigos a devolver
        self.redes["net-external"] = {"id": "net-external", "name": "external", "project_id": "admin"}
        self.imagenes: dict[str, dict] = {
            "img-cirros": {"id": "img-cirros", "name": "cirros"},
        }
        self.flavores: list[dict] = [
            {"id": "flv-512", "name": "512MBRAM_1VCPUs_4GBRoot", "vcpus": 1, "ram": 512, "disk": 4},
        ]
        self.hipervisores = [
            {"hypervisor_hostname": f"worker{i}", "vcpus": 4, "memory_mb": 3935,
             "vcpus_used": 0, "memory_mb_used": 0} for i in range(1, workers + 1)]

    def _id(self, prefijo: str) -> str:
        self._n += 1
        return f"{prefijo}-{self._n}"

    def _usados(self, proyecto: str) -> bool:
        return any(c["project_id"] == proyecto for c in
                   list(self.redes.values()) + list(self.puertos.values()) + list(self.servidores.values()))

    def request(self, metodo, url, headers=None, json=None, params=None, timeout=None):
        ruta = url.split("//", 1)[1].split("/", 1)[1]
        ruta = "/" + ruta
        self.comandos.append((metodo, ruta, json if json is not None else params))
        for (m, frag), codigos in self.fallos_http.items():
            if m == metodo and frag in ruta and codigos:
                return _Resp(codigos.pop(0), {"error": "fallo inyectado"})
        params = params or {}

        def filtrar(coleccion):
            res = list(coleccion.values())
            if "name" in params:
                nombre = params["name"]
                if nombre.startswith("^") and nombre.endswith("$"):
                    res = [x for x in res if x["name"] == nombre[1:-1]]
                else:
                    res = [x for x in res if x["name"] == nombre]
            if params.get("project_id"):
                res = [x for x in res if x["project_id"] == params["project_id"]]
            return res

        # ---- Keystone
        if ruta == "/v3/auth/tokens":
            return _Resp(201, {"token": {}}, {"X-Subject-Token": self._id("tok")})
        if ruta == "/v3/projects" and metodo == "GET":
            return _Resp(200, {"projects": [p for p in self.proyectos.values()
                                             if p["name"] == params.get("name", p["name"])]})
        if ruta == "/v3/projects" and metodo == "POST":
            p = {"id": self._id("proj"), "name": json["project"]["name"]}
            self.proyectos[p["id"]] = p
            return _Resp(201, {"project": p})
        if ruta.startswith("/v3/projects/") and "/roles/" in ruta and metodo == "PUT":
            partes = ruta.split("/")
            self.roles.add((partes[3], partes[5], partes[7]))
            return _Resp(204)
        if ruta.startswith("/v3/projects/") and metodo == "DELETE":
            pid = ruta.split("/")[3]
            if pid not in self.proyectos:
                return _Resp(404)
            if self._usados(pid):
                return _Resp(409, {"error": "el proyecto aún tiene recursos"})
            del self.proyectos[pid]
            return _Resp(204)
        # ---- Neutron
        for nombre, coleccion, clave in (("networks", self.redes, "network"),
                                         ("subnets", self.subredes, "subnet"),
                                         ("ports", self.puertos, "port")):
            base = f"/v2.0/{nombre}"
            if ruta == base and metodo == "GET":
                return _Resp(200, {nombre: filtrar(coleccion)})
            if ruta == base and metodo == "POST":
                item = dict(json[clave])
                item["id"] = self._id(clave)
                item.setdefault("project_id", item.get("tenant_id"))
                coleccion[item["id"]] = item
                return _Resp(201, {clave: item})
            if ruta.startswith(base + "/") and metodo == "DELETE":
                iid = ruta.rsplit("/", 1)[1]
                if iid not in coleccion:
                    return _Resp(404)
                if nombre == "networks" and any(p["network_id"] == iid for p in self.puertos.values()):
                    return _Resp(409, {"error": "la red tiene puertos"})
                del coleccion[iid]
                return _Resp(204)
        # ---- Nova
        if ruta == "/v2.1/flavors/detail" and metodo == "GET":
            return _Resp(200, {"flavors": list(self.flavores)})
        if ruta == "/v2.1/servers" and metodo == "GET":
            return _Resp(200, {"servers": [{"id": s["id"], "name": s["name"]}
                                            for s in filtrar(self.servidores)]})
        if ruta == "/v2.1/servers" and metodo == "POST":
            s = dict(json["server"])
            s["id"] = self._id("srv")
            s["status"] = "ACTIVE"
            s["project_id"] = self.puertos[s["networks"][0]["port"]]["project_id"]
            for p in s["networks"]:
                self.puertos[p["port"]]["device_id"] = s["id"]
            self.servidores[s["id"]] = s
            return _Resp(202, {"server": {"id": s["id"]}})
        if ruta.startswith("/v2.1/servers/") and ruta.endswith("/remote-consoles") and metodo == "POST":
            sid = ruta.split("/")[3]
            return _Resp(200, {"remote_console": {"url": f"http://localhost:56080/vnc_auto.html?path={sid}"}})
        if ruta.startswith("/v2.1/servers/") and metodo == "GET":
            sid = ruta.rsplit("/", 1)[1]
            return _Resp(200, {"server": self.servidores[sid]}) if sid in self.servidores else _Resp(404)
        if ruta.startswith("/v2.1/servers/") and metodo == "DELETE":
            sid = ruta.rsplit("/", 1)[1]
            if sid not in self.servidores:
                return _Resp(404)
            del self.servidores[sid]
            return _Resp(204)
        if ruta == "/v2.1/os-availability-zone/detail":
            hosts = {h["hypervisor_hostname"]: {"nova-compute": {}} for h in self.hipervisores}
            return _Resp(200, {"availabilityZoneInfo": [{"zoneName": "nova", "hosts": hosts}]})
        if ruta == "/v2.1/os-hypervisors/detail":
            return _Resp(200, {"hypervisors": self.hipervisores})
        # ---- Glance
        if ruta == "/v2/images" and metodo == "GET":
            return _Resp(200, {"images": list(self.imagenes.values())})
        return _Resp(404, {"error": f"ruta no simulada: {metodo} {ruta}"})
