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
import math
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .models import CATALOGO_FLAVORS, Host

logger = logging.getLogger(__name__)

PREFIJO_UBICACION = "openstack://"

# Prefijos que identifican, por convención del propio driver (ver
# preparar_slice/crear_red/_resolver_o_crear_flavor_exacto), un recurso
# creado POR el orquestador -- y por lo tanto candidato a "residuo huérfano"
# si ya no lo usa ningún slice. Validado a mano contra el clúster real el
# 12-nov-2026 (ver estado-proyecto, §9quater): cualquier recurso que NO
# empiece con estos prefijos (ej. 'link1'/'link2'/'link3'/'external',
# 'topo1_lab6', '512MBRAM_1VCPUs_4GBRoot') es infraestructura ajena al
# orquestador y 'mantenimiento limpiar-residuos' nunca debe tocarlo.
PREFIJO_PROYECTO_ORQUESTADOR = "orq_"
PREFIJO_RED_ORQUESTADOR = "br-"
PREFIJO_FLAVOR_AUTOMATICO = "auto_"


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
    # Placement vive en un host/puerto propio (no sigue el mismo patron de
    # tunel que keystone/nova/neutron/glance): por defecto el hostname interno
    # del clúster ("controller") en el puerto estandar 8778. Solo resuelve
    # desde dentro del clúster (ej. corriendo en el headnode).
    placement_host: str = "controller"
    puerto_placement: int = 8778
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

    @property
    def placement(self) -> str:
        return f"http://{self.placement_host}:{self.puerto_placement}"

    @classmethod
    def desde_entorno(cls, env: dict | None = None) -> "ConfigOpenStack":
        e = os.environ if env is None else env
        return cls(
            acceso_ip=e.get("ACCESS_NODE_IP", "localhost"),
            puerto_keystone=int(e.get("KEYSTONE_PORT", 55000)),
            puerto_nova=int(e.get("NOVA_PORT", 58774)),
            puerto_neutron=int(e.get("NEUTRON_PORT", 59696)),
            puerto_glance=int(e.get("GLANCE_PORT", 9292)),
            placement_host=e.get("PLACEMENT_HOST", "controller"),
            puerto_placement=int(e.get("PLACEMENT_PORT", 8778)),
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
        """Elimina el proyecto del slice (``orq_<slice_id>``, siempre exclusivo
        de ese slice por construcción -- ver preparar_slice).

        Antes se exigía además ``self._proyecto_creado`` (que ESTA instancia lo
        haya creado), pero esa bandera vive solo en memoria de un objeto en
        particular: si 'crear'/'desplegar' y 'eliminar' corren en procesos
        distintos de la CLI (el caso real, uno por invocación), la instancia
        que elimina siempre lo redescubre vía preparar_slice() con
        ``_proyecto_creado = False`` y nunca lo borraría. El nombre determina
        la propiedad, no quién lo creó, así que borrar siempre que haya un
        ``proyecto_id`` resuelto para este slice es seguro."""
        if self.proyecto_id:
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

    def _listar_flavors(self) -> list[dict]:
        """Lista los flavors que ya existen en Nova. Aparte para reutilizarla
        desde sincronizar_catalogo_flavors() y desde la creación automática."""
        try:
            return self._api("admin", "GET", self.config.nova + "/flavors/detail").json().get("flavors", [])
        except DriverOpenStackError as exc:
            logger.warning("No se pudo listar los flavors de Nova: %s", exc)
            return []

    def _resolver_o_crear_flavor_exacto(self, vcpu: int, ram_mb: int, disco_gb: float) -> str | None:
        """Busca un flavor de Nova con EXACTAMENTE el vCPU/RAM/disco pedidos
        y, si no existe ninguno, lo crea (requiere privilegios de admin, que
        ya usamos para todo lo demás en este driver).

        A diferencia de la versión anterior de ``sincronizar_catalogo_flavors``
        (que aceptaba cualquier flavor con disco >= al pedido), esto evita que
        una VM termine con MÁS disco real del que el scheduler calculó al
        decidir dónde ubicarla -- encontrado en pruebas reales contra el
        clúster del curso: se pedían VMs de 1 GB y, al no existir un flavor
        de exactamente 1 GB, el driver reutilizaba uno de 4 GB, haciendo que
        el scheduler subestimara cuánto disco real iba a consumir cada VM.

        Nova exige el disco en GB enteros, así que redondeamos hacia arriba
        (nunca hacia abajo, para no quedarnos cortos frente a lo que la
        imagen necesita) con ``math.ceil``.
        """
        disco_entero = math.ceil(disco_gb)
        disponibles = self._listar_flavors()
        exacto = next(
            (f for f in disponibles
             if f["vcpus"] == vcpu and f["ram"] == ram_mb and f["disk"] == disco_entero),
            None,
        )
        if exacto:
            return exacto["id"]
        nombre = f"auto_{vcpu}vcpu_{ram_mb}mb_{disco_entero}gb"
        try:
            r = self._api("admin", "POST", self.config.nova + "/flavors", cuerpo={
                "flavor": {"name": nombre, "vcpus": vcpu, "ram": ram_mb, "disk": disco_entero},
            }, esperado=(200, 201))
        except DriverOpenStackError as exc:
            logger.warning("No se pudo crear el flavor '%s' en Nova: %s", nombre, exc)
            return None
        nuevo = r.json().get("flavor", {})
        if nuevo.get("id"):
            logger.info("[OPENSTACK] flavor creado automáticamente en Nova: %s (id=%s, "
                        "%s vCPU / %s MB / %s GB)", nombre, nuevo["id"], vcpu, ram_mb, disco_entero)
        return nuevo.get("id")

    def sincronizar_catalogo_flavors(self, forzar: bool = False) -> dict[tuple[int, int, float], str]:
        """Resuelve automáticamente `flavor_por_perfil` para cada flavor del
        catálogo (``models.CATALOGO_FLAVORS``), consultando Nova y creando el
        flavor exacto si hace falta (ver ``_resolver_o_crear_flavor_exacto``),
        para que el usuario final no tenga que conocer de antemano los IDs
        de flavor ni depender de que el clúster ya tenga uno del tamaño
        correcto.

        Un mapeo puesto a mano en `flavor_por_perfil` al construir el driver
        NO se pisa (tiene prioridad): esto solo RELLENA lo que falte. Si de
        verdad no se puede resolver ni crear nada (ej. sin permisos), usa el
        primer flavor disponible de Nova como `flavor_id_defecto`, para no
        bloquear la demo.
        """
        if self.flavor_por_perfil and not forzar:
            return self.flavor_por_perfil
        for flavor in CATALOGO_FLAVORS.values():
            clave = (flavor.vcpu, flavor.ram_mb, flavor.disco_gb)
            if clave in self.flavor_por_perfil:
                continue
            id_ = self._resolver_o_crear_flavor_exacto(flavor.vcpu, flavor.ram_mb, flavor.disco_gb)
            if id_:
                self.flavor_por_perfil[clave] = id_
        if self.flavor_id_defecto is None:
            disponibles = self._listar_flavors()
            if disponibles:
                self.flavor_id_defecto = disponibles[0]["id"]
        logger.info("[OPENSTACK] catálogo de flavors sincronizado desde Nova: %s "
                    "(defecto=%s)", self.flavor_por_perfil, self.flavor_id_defecto)
        return self.flavor_por_perfil

    def _resolver_flavor(self, vcpu: int, ram_mb: int, disco_gb: float) -> str:
        clave = (vcpu, ram_mb, disco_gb)
        id_ = self.flavor_por_perfil.get(clave)
        if not id_ and self.modo == ModoOpenStack.REAL:
            # Nadie configuró un mapeo manual: resolvemos o creamos nosotros
            # mismos el flavor exacto contra Nova, antes de rendirnos
            # (automatización end-to-end -- ver _resolver_o_crear_flavor_exacto).
            id_ = self._resolver_o_crear_flavor_exacto(vcpu, ram_mb, disco_gb)
            if id_:
                self.flavor_por_perfil[clave] = id_
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

    def verificar_vm(self, host_uri: str, nombre_vm: str) -> str:
        """Consulta el estado REAL de la instancia en Nova. crear_vm() solo
        confirma que Nova ACEPTO la peticion (202); la VM se termina de
        construir en segundo plano y puede terminar en ERROR (por ejemplo,
        NoValidHost real detectado recien al intentar reservar el disco en
        el host elegido -- visto en el clúster del curso el 12-nov con la
        estrategia ROUND_ROBIN, que no revisa disco al elegir host).

        Devuelve el ``status`` tal cual lo reporta Nova ("ACTIVE", "ERROR",
        "BUILD", etc.). Si la instancia ya no aparece (se borró sola o nunca
        llegó a crearse del todo), se trata como "ERROR": no puede estar
        lista si ya no existe."""
        c = self.config
        r = self._api("proyecto", "GET", c.nova + "/servers", params={"name": f"^{nombre_vm}$"})
        servidores = r.json().get("servers", [])
        if not servidores:
            return "ERROR"
        detalle = self._api("proyecto", "GET", f"{c.nova}/servers/{servidores[0]['id']}", tolerar=(404,))
        if detalle.status_code == 404:
            return "ERROR"
        return detalle.json()["server"].get("status", "UNKNOWN")

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

    def _consultar_placement(self, nombre_host: str) -> dict[str, tuple[float, float]] | None:
        """Consulta la fuente de verdad real que usa Nova para aceptar o
        rechazar una VM (servicio Placement), no la vista administrativa
        vieja de ``os-hypervisors/detail``.

        Devuelve, por clase de recurso ('VCPU', 'MEMORY_MB', 'DISK_GB'), la
        tupla (capacidad_efectiva, usado), donde capacidad_efectiva ya
        aplica ``(total - reserved) * allocation_ratio`` tal como lo hace
        Nova. Devuelve ``None`` si Placement no responde o el host no
        aparece registrado ahi (se usa como respaldo os-hypervisors/detail,
        ver descubrir_hosts()).
        """
        cabecera = {"OpenStack-API-Version": "placement latest"}
        try:
            r = self._api("admin", "GET", self.config.placement + "/resource_providers",
                           params={"name": nombre_host}, extra=cabecera)
            proveedores = r.json().get("resource_providers", [])
            if not proveedores:
                return None
            uuid = proveedores[0]["uuid"]
            inv = self._api("admin", "GET",
                             f"{self.config.placement}/resource_providers/{uuid}/inventories",
                             extra=cabecera).json().get("inventories", {})
            uso = self._api("admin", "GET",
                             f"{self.config.placement}/resource_providers/{uuid}/usages",
                             extra=cabecera).json().get("usages", {})
        except (DriverOpenStackError, ValueError, KeyError) as exc:
            logger.warning("No se pudo consultar Placement para %s: %s", nombre_host, exc)
            return None

        resultado: dict[str, tuple[float, float]] = {}
        for clase, datos in inv.items():
            total = float(datos.get("total", 0))
            reservado = float(datos.get("reserved", 0))
            ratio = float(datos.get("allocation_ratio", 1.0))
            capacidad_efectiva = (total - reservado) * ratio
            usado = float(uso.get(clase, 0))
            resultado[clase] = (capacidad_efectiva, usado)
        return resultado

    def descubrir_hosts(self) -> list[Host]:
        """Inventario de los workers (para VM Placement y monitoreo).

        Primero intenta leer el estado REAL de cada host desde Placement
        (la fuente que Nova realmente usa para aceptar o rechazar una VM,
        via ``select_destinations`` -> allocation candidates). Si Placement
        no responde o no tiene registrado un host, cae de vuelta a la vista
        administrativa de Nova (``os-hypervisors/detail``), igual que antes.

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

            # Valores de respaldo: vista administrativa vieja de Nova.
            capacidad_vcpu, vcpu_usados = float(h["vcpus"]), float(h.get("vcpus_used", 0))
            capacidad_ram_mb, ram_usados_mb = float(h["memory_mb"]), float(h.get("memory_mb_used", 0))
            # Disco: si Nova no informa local_gb (queda en 0), el
            # FiltroCapacidadDisco se auto-desactiva (ver models.py).
            capacidad_disco_gb, disco_usado_gb = float(h.get("local_gb", 0)), float(h.get("local_gb_used", 0))

            # OJO: Placement ya aplica su propio allocation_ratio al calcular
            # la capacidad efectiva (ver _consultar_placement). Si se usa este
            # Host con un overcommit_vcpu/overcommit_ram adicional (>1.0) en
            # el scheduler, el overcommit se aplicaria DOS veces. Con el
            # driver real, dejar overcommit_vcpu/overcommit_ram en 1.0 (el
            # valor por defecto) y confiar en lo que ya informa Placement.
            placement = self._consultar_placement(nombre)
            if placement:
                if "VCPU" in placement:
                    capacidad_vcpu, vcpu_usados = placement["VCPU"]
                if "MEMORY_MB" in placement:
                    capacidad_ram_mb, ram_usados_mb = placement["MEMORY_MB"]
                if "DISK_GB" in placement:
                    capacidad_disco_gb, disco_usado_gb = placement["DISK_GB"]

            hosts.append(Host(
                id=nombre, nombre=nombre, uri_libvirt=ubicacion(zona, nombre),
                capacidad_vcpu=int(capacidad_vcpu), capacidad_ram_mb=int(capacidad_ram_mb),
                zona_disponibilidad=zona,
                vcpu_usados=int(vcpu_usados), ram_usados_mb=int(ram_usados_mb),
                capacidad_disco_gb=capacidad_disco_gb,
                disco_usado_gb=disco_usado_gb))
        return hosts

    # ------------------------------------------------------- limpieza de residuos
    def listar_residuos(self) -> dict[str, list[dict]]:
        """Detecta (SIN borrar nada) recursos huérfanos dejados por sesiones
        anteriores -- típicamente de un 'slice eliminar' que falló a mitad de
        camino, o de un borrado manual con 'openstack server delete' que no
        pasó por este driver (ver estado-proyecto, §9quater, hallazgo del
        12-nov-2026). Usa exactamente los criterios validados a mano contra
        el clúster real ese día:

          - proyectos 'orq_<slice_id>' (Keystone) sin NINGUNA instancia
            activa (en cualquier proyecto -- ``all_tenants``).
          - redes 'br-*' (Neutron) cuyos puertos no apuntan (``device_id``) a
            ninguna instancia que exista hoy -- señal de que quedaron
            "colgando" tras un borrado que no pasó por ``eliminar_vm``/
            ``eliminar_red``.
          - flavors 'auto_*' (Nova, creados por
            ``_resolver_o_crear_flavor_exacto``) que ninguna instancia actual
            referencia.

        Nunca toca ni reporta un recurso que no calce con estos prefijos
        (infraestructura ajena al orquestador: redes 'link1/2/3/external',
        proyectos de otros labs, flavors creados a mano)."""
        c = self.config
        proyectos = self._api("admin", "GET", c.keystone + "/projects").json().get("projects", [])
        servidores = self._api("admin", "GET", c.nova + "/servers/detail",
                                params={"all_tenants": True}).json().get("servers", [])
        ids_servidores_activos = {s["id"] for s in servidores}
        proyectos_con_servidor = {s["project_id"] for s in servidores if s.get("project_id")}

        reporte: dict[str, list[dict]] = {"proyectos": [], "redes": [], "flavors": []}

        for p in proyectos:
            nombre = p.get("name", "")
            if nombre.startswith(PREFIJO_PROYECTO_ORQUESTADOR) and p["id"] not in proyectos_con_servidor:
                reporte["proyectos"].append({"id": p["id"], "name": nombre})

        redes = self._api("admin", "GET", c.neutron + "/networks").json().get("networks", [])
        puertos = self._api("admin", "GET", c.neutron + "/ports").json().get("ports", [])
        for red in redes:
            nombre = red.get("name", "")
            if not nombre.startswith(PREFIJO_RED_ORQUESTADOR):
                continue
            puertos_de_red = [pt for pt in puertos if pt.get("network_id") == red["id"]]
            en_uso = any(pt.get("device_id") in ids_servidores_activos for pt in puertos_de_red)
            if not en_uso:
                reporte["redes"].append({"id": red["id"], "name": nombre,
                                          "puertos": [pt["id"] for pt in puertos_de_red]})

        flavors_en_uso: set[str] = set()
        for s in servidores:
            flv = s.get("flavor")
            fid = flv.get("id") if isinstance(flv, dict) else (flv or s.get("flavorRef"))
            if fid:
                flavors_en_uso.add(fid)
        for f in self._listar_flavors():
            nombre = f.get("name", "")
            if nombre.startswith(PREFIJO_FLAVOR_AUTOMATICO) and f["id"] not in flavors_en_uso:
                reporte["flavors"].append({"id": f["id"], "name": nombre})

        return reporte

    def limpiar_residuos(self, confirmar: bool = False) -> dict[str, list[dict]]:
        """``listar_residuos()`` y, si ``confirmar=True``, además BORRA cada
        recurso huérfano detectado, en el orden seguro -- puertos, luego
        subredes, luego redes, luego flavors y por último proyectos (un
        proyecto no se puede borrar si todavía tiene recursos propios, igual
        que en ``liberar_slice``). Con ``confirmar=False`` (por defecto) es un
        dry-run: informa lo que borraría sin tocar el clúster."""
        reporte = self.listar_residuos()
        if not confirmar:
            return reporte
        c = self.config
        for red in reporte["redes"]:
            for puerto_id in red.get("puertos", []):
                self._api("admin", "DELETE", f"{c.neutron}/ports/{puerto_id}", esperado=(204,), tolerar=(404,))
            subredes = self._api("admin", "GET", c.neutron + "/subnets",
                                  params={"network_id": red["id"]}).json().get("subnets", [])
            for sub in subredes:
                self._api("admin", "DELETE", f"{c.neutron}/subnets/{sub['id']}", esperado=(204,), tolerar=(404,))
            self._api("admin", "DELETE", f"{c.neutron}/networks/{red['id']}", esperado=(204,), tolerar=(404,))
        for flavor in reporte["flavors"]:
            # A diferencia de 'servers' (que responde 204), la API real de Nova
            # responde 202 Accepted al borrar un flavor -- confirmado contra el
            # clúster real de PUCP el 13-nov-2026 (antes asumíamos 204 por
            # simetría con el resto de borrados, y el clúster real lo rechazó).
            self._api("admin", "DELETE", f"{c.nova}/flavors/{flavor['id']}",
                      esperado=(202, 204), tolerar=(404,))
        for proyecto in reporte["proyectos"]:
            self._api("admin", "DELETE", f"{c.keystone}/projects/{proyecto['id']}",
                      esperado=(204,), tolerar=(404,))
        return reporte


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
            if params.get("network_id"):
                res = [x for x in res if x.get("network_id") == params["network_id"]]
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
        if ruta == "/v2.1/flavors" and metodo == "POST":
            item = dict(json["flavor"])
            item["id"] = self._id("flv")
            self.flavores.append(item)
            return _Resp(201, {"flavor": item})
        if ruta.startswith("/v2.1/flavors/") and metodo == "DELETE":
            fid = ruta.rsplit("/", 1)[1]
            antes = len(self.flavores)
            self.flavores = [f for f in self.flavores if f["id"] != fid]
            # Nova real responde 202 (no 204) al borrar un flavor -- ver
            # limpiar_residuos(), confirmado contra el clúster real el 13-nov.
            return _Resp(202) if len(self.flavores) < antes else _Resp(404)
        if ruta == "/v2.1/servers" and metodo == "GET":
            return _Resp(200, {"servers": [{"id": s["id"], "name": s["name"]}
                                            for s in filtrar(self.servidores)]})
        if ruta == "/v2.1/servers/detail" and metodo == "GET":
            return _Resp(200, {"servers": filtrar(self.servidores)})
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
