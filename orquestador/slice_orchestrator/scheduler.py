"""
scheduler.py
------------
Scheduler inteligente de VMs (componente tipo nova-scheduler).

Responsabilidad: dado un slice con N nodos por desplegar y un inventario de
hosts, decidir en que host fisico va cada VM, explicando cada decision.

Arquitectura interna (inspirada en nova-scheduler de OpenStack):

    1. ResourceMonitor   -- refresca el estado real de los hosts (vCPU, RAM,
                            disco usados) consultando al driver (Nova o libvirt).
    2. Filtros           -- descartan hosts que no sirven (zona, capacidad,
                            overcommit, host activo).
    3. Pesadores         -- puntuan a los hosts que pasan los filtros
                            (balance de recursos, afinidad topologica).
    4. Funcion objetivo  -- suma ponderada de los pesadores.
    5. Traza             -- registro explicable de cada decision.

Congestion futura (no solo la utilizacion medida al momento de decidir):

    - RegistroReservas      -- recuerda las asignaciones recien confirmadas
                               que la medicion del driver todavia no refleja,
                               y las suma al uso medido hasta que aparezcan.
    - FiltroCongestionFutura -- modela la demanda de cada recurso asignado como
                               una variable aleatoria normal y exige que la
                               demanda prevista (mu + z*sigma) no supere la
                               capacidad fisica con probabilidad mayor al SLA.

Limite de tiempo: si el calculo de un slice grande supera
``ConfigScheduler.tiempo_limite_s``, los nodos que faltan se asignan en modo
rapido (mismos filtros, sin pesadores) y la traza lo deja registrado.

El scheduler NO ejecuta nada en la infraestructura: solo decide y devuelve
la asignacion. La ejecucion sigue en el orchestrator via el driver.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any, Callable, Protocol, runtime_checkable

from .models import Slice, Host, Nodo

logger = logging.getLogger("scheduler")


# ========================== TRAZA / EXPLICACION ==============================

@dataclass
class CandidatoEvaluado:
    """Resultado de la evaluacion de un host para un nodo concreto."""
    host_id: str
    paso_filtros: bool
    filtros_rechazados: list[str] = field(default_factory=list)
    puntajes: dict[str, float] = field(default_factory=dict)
    puntaje_total: float = 0.0


@dataclass
class DecisionVM:
    """Traza de la decision de placement para un nodo."""
    nodo_id: str
    nodo_nombre: str
    host_elegido: str | None = None
    candidatos: list[CandidatoEvaluado] = field(default_factory=list)
    razon: str = ""


@dataclass
class ResultadoScheduler:
    """Resultado completo del scheduler: asignacion + explicacion."""
    asignacion: dict[str, str]          # nodo_id -> host_id
    decisiones: list[DecisionVM] = field(default_factory=list)
    timestamp: float = 0.0
    duracion_s: float = 0.0             # tiempo de calculo del scheduler
    nodos_modo_rapido: list[str] = field(default_factory=list)  # nodo_id asignados tras el limite de tiempo

    @property
    def limite_de_tiempo_alcanzado(self) -> bool:
        return bool(self.nodos_modo_rapido)

    def explicacion_texto(self) -> str:
        """Resumen legible de todas las decisiones."""
        lineas = []
        if self.nodos_modo_rapido:
            lineas.append(
                f"  [limite de tiempo] {len(self.nodos_modo_rapido)} nodo(s) "
                f"asignados en modo rapido (filtros sin pesadores)")
        for d in self.decisiones:
            aprobados = [c for c in d.candidatos if c.paso_filtros]
            rechazados = [c for c in d.candidatos if not c.paso_filtros]
            linea = f"  {d.nodo_nombre}: "
            if rechazados:
                motivos = {r: [] for c in rechazados for r in c.filtros_rechazados}
                for c in rechazados:
                    for r in c.filtros_rechazados:
                        motivos[r].append(c.host_id)
                partes_rechazo = [f"{r} ({', '.join(hs)})" for r, hs in motivos.items()]
                linea += f"descartados por {'; '.join(partes_rechazo)}. "
            if aprobados:
                ranking = sorted(aprobados, key=lambda c: c.puntaje_total, reverse=True)
                scores = ", ".join(f"{c.host_id}={c.puntaje_total:.2f}" for c in ranking)
                linea += f"Puntuacion: [{scores}]. "
            linea += f"Elegido: {d.host_elegido}."
            lineas.append(linea)
        return "Decisiones del scheduler:\n" + "\n".join(lineas)


# ===================== RESERVAS RECIENTES (congestion futura) ================

def _float_entorno(nombre: str, defecto: float) -> float:
    """Lee un float de una variable de entorno; si falta o es invalido, usa el defecto."""
    valor = os.environ.get(nombre)
    if valor is None or valor.strip() == "":
        return defecto
    try:
        return float(valor)
    except ValueError:
        logger.warning("Variable %s=%r no es un numero; se usa %s", nombre, valor, defecto)
        return defecto


@dataclass
class _Reserva:
    clave: str
    host_id: str
    vcpu: float
    ram_mb: float
    disco_gb: float
    creada: float
    # Uso medido del host cuando se hizo la reserva (None si nunca se midio).
    base: tuple | None = None


class RegistroReservas:
    """Asignaciones confirmadas que la medicion del driver todavia no refleja.

    Problema que resuelve: el ResourceMonitor sobreescribe el uso de cada host
    con lo que informa el driver (Nova o libvirt). Si un slice se acaba de
    confirmar y sus VMs aun no aparecen en esa lectura (Nova actualiza sus
    contadores con retraso, o las VMs siguen creandose), el siguiente slice
    veria esa capacidad como libre y podria sobrecargar el host.

    Cada reserva se suma al uso medido hasta que ocurre lo primero de:
      - la medicion del host crece al menos lo reservado desde que se hizo
        la reserva (ya se refleja: sumarla otra vez la contaria dos veces), o
      - vence su tiempo de vida (``ttl_s``), o
      - se libera explicitamente (rollback o eliminacion del slice).

    Es compartido por todo el proceso (``REGISTRO_RESERVAS``) porque cada
    llamada a vm_placement crea un Scheduler nuevo. Es seguro entre hilos.
    """

    def __init__(self, ttl_s: float | None = None,
                 reloj: Callable[[], float] = time.monotonic):
        self._ttl_fijo = ttl_s
        self._reloj = reloj
        self._reservas: dict[str, _Reserva] = {}
        self._ultima_medicion: dict[str, tuple] = {}
        self._lock = threading.Lock()

    @property
    def ttl_s(self) -> float:
        if self._ttl_fijo is not None:
            return self._ttl_fijo
        return _float_entorno("ORQ_SCHEDULER_TTL_RESERVA", 180.0)

    def reservar(self, clave: str, host_id: str, vcpu: float, ram_mb: float,
                 disco_gb: float) -> None:
        with self._lock:
            self._reservas[clave] = _Reserva(
                clave=clave, host_id=host_id, vcpu=vcpu, ram_mb=ram_mb,
                disco_gb=disco_gb, creada=self._reloj(),
                base=self._ultima_medicion.get(host_id),
            )

    def liberar(self, claves) -> None:
        with self._lock:
            for clave in claves:
                self._reservas.pop(clave, None)

    def limpiar(self) -> None:
        with self._lock:
            self._reservas.clear()
            self._ultima_medicion.clear()

    def reservas_de(self, host_id: str) -> list[_Reserva]:
        with self._lock:
            return [r for r in self._reservas.values() if r.host_id == host_id]

    def aplicar_medicion(self, host_id: str, vcpu_medido: float,
                         ram_medida_mb: float, disco_medido_gb: float) -> tuple:
        """Registra una medicion nueva del host y devuelve cuanto hay que
        sumarle por reservas que todavia no se reflejan: (vcpu, ram_mb, disco_gb).

        Descarta las reservas vencidas y las que la medicion ya muestra.
        """
        ahora = self._reloj()
        ttl = self.ttl_s
        medido = (vcpu_medido, ram_medida_mb, disco_medido_gb)
        with self._lock:
            del_host = sorted(
                (r for r in self._reservas.values() if r.host_id == host_id),
                key=lambda r: r.creada)
            acumulado = [0.0, 0.0, 0.0]
            pendiente = [0.0, 0.0, 0.0]
            for r in del_host:
                if ahora - r.creada > ttl:
                    del self._reservas[r.clave]
                    continue
                cantidades = (r.vcpu, r.ram_mb, r.disco_gb)
                for i in range(3):
                    acumulado[i] += cantidades[i]
                # Se considera reflejada si, desde su base, la medicion crecio al
                # menos lo reservado por ella y por las anteriores aun vigentes.
                # El disco no se exige: algunos drivers no lo informan (queda en 0).
                if r.base is not None and all(
                        medido[i] - r.base[i] >= acumulado[i] for i in (0, 1)):
                    del self._reservas[r.clave]
                    continue
                for i in range(3):
                    pendiente[i] += cantidades[i]
            self._ultima_medicion[host_id] = medido
        return tuple(pendiente)


REGISTRO_RESERVAS = RegistroReservas()


# ===================== MODELO ESTOCASTICO (congestion futura) ================

@dataclass
class ModeloCongestion:
    """Demanda futura de los recursos asignados a un host (placement estocastico).

    Cada unidad asignada (1 vCPU, o ``unidad_ram_mb`` de RAM) se modela como
    una variable aleatoria normal: en promedio usa ``media`` de su valor
    nominal, con desviacion ``desv``. Por el teorema del limite central, la
    demanda de ``a`` unidades en un host es normal con

        mu = media * a          sigma = desv * sqrt(a)

    y la demanda prevista con el SLA pedido es mu + z * sigma, donde z es el
    cuantil de la normal tal que P(demanda > prevista) = ``prob_desborde``.
    La demanda prevista nunca supera lo asignado (una VM no usa mas de lo
    que tiene), asi que sin overcommit el filtro nunca rechaza: solo actua
    cuando el overcommit permite asignar mas de lo que el host tiene.

    Los parametros son estimaciones configurables (se calibran con datos de
    monitoreo): CPU con media baja y overcommit agresivo, RAM con media alta,
    disco sin overcommit (no se modela: lo controla FiltroCapacidadDisco).
    """
    prob_desborde: float = 0.05
    media_vcpu: float = 0.30
    desv_vcpu: float = 0.20
    media_ram: float = 0.75
    desv_ram: float = 0.15
    unidad_ram_mb: float = 1024.0
    umbral: float = 1.0   # fraccion de la capacidad fisica que no se debe superar

    @classmethod
    def desde_entorno(cls) -> "ModeloCongestion":
        """Lee ORQ_CONGESTION_* (si faltan, usa los valores por defecto)."""
        d = cls()
        return cls(
            prob_desborde=_float_entorno("ORQ_CONGESTION_PROB_DESBORDE", d.prob_desborde),
            media_vcpu=_float_entorno("ORQ_CONGESTION_MEDIA_VCPU", d.media_vcpu),
            desv_vcpu=_float_entorno("ORQ_CONGESTION_DESV_VCPU", d.desv_vcpu),
            media_ram=_float_entorno("ORQ_CONGESTION_MEDIA_RAM", d.media_ram),
            desv_ram=_float_entorno("ORQ_CONGESTION_DESV_RAM", d.desv_ram),
            unidad_ram_mb=_float_entorno("ORQ_CONGESTION_UNIDAD_RAM_MB", d.unidad_ram_mb),
            umbral=_float_entorno("ORQ_CONGESTION_UMBRAL", d.umbral),
        )

    def __post_init__(self) -> None:
        if not 0.0 < self.prob_desborde < 1.0:
            raise ValueError("prob_desborde debe estar entre 0 y 1 (exclusivo)")
        for nombre in ("media_vcpu", "desv_vcpu", "media_ram", "desv_ram"):
            if getattr(self, nombre) < 0:
                raise ValueError(f"{nombre} no puede ser negativo")
        if self.unidad_ram_mb <= 0 or self.umbral <= 0:
            raise ValueError("unidad_ram_mb y umbral deben ser positivos")

    @property
    def z(self) -> float:
        return NormalDist().inv_cdf(1.0 - self.prob_desborde)

    def _prevista(self, unidades: float, media: float, desv: float) -> float:
        if unidades <= 0:
            return 0.0
        return min(unidades, media * unidades + self.z * desv * math.sqrt(unidades))

    def vcpu_prevista(self, vcpu_asignados: float) -> float:
        """Demanda prevista de vCPU (en vCPU) para lo asignado al host."""
        return self._prevista(vcpu_asignados, self.media_vcpu, self.desv_vcpu)

    def ram_prevista_mb(self, ram_asignada_mb: float) -> float:
        """Demanda prevista de RAM (en MB) para lo asignado al host."""
        u = self.unidad_ram_mb
        return self._prevista(ram_asignada_mb / u, self.media_ram, self.desv_ram) * u

    def congestion_prevista(self, host: Host, vcpu_extra: float = 0.0,
                            ram_extra_mb: float = 0.0) -> tuple:
        """Fraccion (vcpu, ram) de la capacidad fisica que se preve usar."""
        f_vcpu = (self.vcpu_prevista(host.vcpu_usados + vcpu_extra) / host.capacidad_vcpu
                  if host.capacidad_vcpu > 0 else 0.0)
        f_ram = (self.ram_prevista_mb(host.ram_usados_mb + ram_extra_mb) / host.capacidad_ram_mb
                 if host.capacidad_ram_mb > 0 else 0.0)
        return f_vcpu, f_ram


# ========================== MONITOR DE RECURSOS ==============================

@runtime_checkable
class ProveedorEstado(Protocol):
    """Interfaz que debe cumplir un driver para alimentar al monitor.
    Basta con implementar descubrir_hosts()."""
    def descubrir_hosts(self) -> list[Host]: ...


class ResourceMonitor:
    """Lee el estado real de los hosts desde el driver y actualiza el inventario.

    Modos:
    - Con driver (OpenStack/Linux): lee vcpus_used, memory_mb_used reales de Nova
      o de libvirt. Esto cuenta TODAS las VMs, no solo las del orquestador.
    - Sin driver: usa el inventario registrado (conteo interno del orquestador).

    Con driver, al uso medido se le suman las reservas recientes que la
    medicion todavia no refleja (ver RegistroReservas). Sin driver no hace
    falta: el inventario interno ya las cuenta.
    """

    def __init__(self, driver: ProveedorEstado | None = None,
                 staleness_s: float = 60.0,
                 reservas: RegistroReservas | None = None):
        self._driver = driver
        self._staleness_s = staleness_s
        self._ultimo_refresco: float = 0.0
        self._reservas = reservas if reservas is not None else REGISTRO_RESERVAS

    def refrescar(self, hosts: list[Host], forzar: bool = False) -> list[Host]:
        """Actualiza el uso real de cada host desde el driver.
        Si no hay driver o si la medicion es reciente, no hace nada."""
        ahora = time.time()
        if not forzar and (ahora - self._ultimo_refresco) < self._staleness_s:
            return hosts
        if self._driver is None:
            return hosts

        try:
            hosts_reales = self._driver.descubrir_hosts()
        except Exception as exc:
            logger.warning("Monitor: no se pudo leer el estado de los hosts: %s", exc)
            return hosts

        reales_por_id = {h.id: h for h in hosts_reales}
        for h in hosts:
            real = reales_por_id.get(h.id)
            if real is not None:
                p_vcpu, p_ram, p_disco = self._reservas.aplicar_medicion(
                    h.id, real.vcpu_usados, real.ram_usados_mb, real.disco_usado_gb)
                if p_vcpu or p_ram or p_disco:
                    logger.info("Monitor: %s suma reservas aun no medidas "
                                "(%s vCPU, %s MB, %s GB)", h.id, p_vcpu, p_ram, p_disco)
                h.vcpu_usados = real.vcpu_usados + p_vcpu
                h.ram_usados_mb = real.ram_usados_mb + p_ram
                h.disco_usado_gb = real.disco_usado_gb + p_disco
                h.ultima_medicion = ahora
                # Actualizar capacidad si cambio (ej: host reconfigurable)
                h.capacidad_vcpu = real.capacidad_vcpu
                h.capacidad_ram_mb = real.capacidad_ram_mb
                h.capacidad_disco_gb = real.capacidad_disco_gb
        self._ultimo_refresco = ahora
        logger.info("Monitor: estado de %d hosts actualizado", len(hosts))
        return hosts


# ============================== FILTROS ======================================

class Filtro:
    """Filtro base. Devuelve True si el host es apto para el nodo."""
    nombre: str = "base"

    def aceptar(self, host: Host, nodo: Nodo, contexto: dict[str, Any]) -> bool:
        return True


class FiltroZonaDisponibilidad(Filtro):
    """Descarta hosts que no pertenecen a la zona del slice."""
    nombre = "zona_disponibilidad"

    def aceptar(self, host: Host, nodo: Nodo, contexto: dict[str, Any]) -> bool:
        zona = contexto.get("zona_disponibilidad")
        if zona is None:
            return True
        return host.zona_disponibilidad == zona


class FiltroHostActivo(Filtro):
    """Descarta hosts marcados como inactivos / en mantenimiento."""
    nombre = "host_activo"

    def aceptar(self, host: Host, nodo: Nodo, contexto: dict[str, Any]) -> bool:
        return host.activo


class FiltroCapacidadVCPU(Filtro):
    """Descarta hosts sin vCPU suficientes (con overcommit)."""
    nombre = "capacidad_vcpu"

    def aceptar(self, host: Host, nodo: Nodo, contexto: dict[str, Any]) -> bool:
        return host.vcpu_disponibles_con_overcommit >= nodo.vcpu


class FiltroCapacidadRAM(Filtro):
    """Descarta hosts sin RAM suficiente (con overcommit)."""
    nombre = "capacidad_ram"

    def aceptar(self, host: Host, nodo: Nodo, contexto: dict[str, Any]) -> bool:
        return host.ram_disponible_con_overcommit_mb >= nodo.ram_mb


class FiltroCapacidadDisco(Filtro):
    """Descarta hosts sin disco suficiente. Se ignora si capacidad_disco_gb == 0."""
    nombre = "capacidad_disco"

    def aceptar(self, host: Host, nodo: Nodo, contexto: dict[str, Any]) -> bool:
        if host.capacidad_disco_gb <= 0:
            return True  # disco no informado, no filtrar
        return host.disco_disponible_gb >= nodo.disco_gb


class FiltroCongestionFutura(Filtro):
    """Descarta hosts cuya demanda prevista (mu + z*sigma) superaria la
    capacidad fisica con probabilidad mayor a la del SLA.

    Complementa a los filtros de capacidad: esos limitan cuanto se asigna
    (overcommit maximo); este limita cuanto se espera que se USE, dejando
    margen para que VMs con baja utilizacion hoy puedan crecer. Usa el
    ModeloCongestion del contexto (``contexto["modelo_congestion"]``) o, si
    no hay, el propio.
    """
    nombre = "congestion_futura"

    def __init__(self, modelo: ModeloCongestion | None = None):
        self.modelo = modelo

    def aceptar(self, host: Host, nodo: Nodo, contexto: dict[str, Any]) -> bool:
        modelo = contexto.get("modelo_congestion") or self.modelo
        if modelo is None:
            return True
        f_vcpu, f_ram = modelo.congestion_prevista(host, nodo.vcpu, nodo.ram_mb)
        return f_vcpu <= modelo.umbral + 1e-9 and f_ram <= modelo.umbral + 1e-9


FILTROS_POR_DEFECTO: list[Filtro] = [
    FiltroZonaDisponibilidad(),
    FiltroHostActivo(),
    FiltroCapacidadVCPU(),
    FiltroCapacidadRAM(),
    FiltroCapacidadDisco(),
    FiltroCongestionFutura(),
]


# ============================== PESADORES ====================================

class Pesador:
    """Pesador base. Devuelve un float en [0, 1] para un host dado un nodo."""
    nombre: str = "base"
    peso: float = 1.0

    def puntuar(self, host: Host, nodo: Nodo, contexto: dict[str, Any]) -> float:
        return 0.0


class PesadorBalanceVCPU(Pesador):
    """Prefiere hosts con mas vCPU libres (proporcionalmente)."""
    nombre = "balance_vcpu"

    def __init__(self, peso: float = 1.0):
        self.peso = peso

    def puntuar(self, host: Host, nodo: Nodo, contexto: dict[str, Any]) -> float:
        if host.vcpu_efectivos <= 0:
            return 0.0
        return host.vcpu_disponibles_con_overcommit / host.vcpu_efectivos


class PesadorBalanceRAM(Pesador):
    """Prefiere hosts con mas RAM libre (proporcionalmente)."""
    nombre = "balance_ram"

    def __init__(self, peso: float = 1.0):
        self.peso = peso

    def puntuar(self, host: Host, nodo: Nodo, contexto: dict[str, Any]) -> float:
        if host.ram_efectiva_mb <= 0:
            return 0.0
        return host.ram_disponible_con_overcommit_mb / host.ram_efectiva_mb


class PesadorAfinidadTopologica(Pesador):
    """Prefiere hosts donde ya estan los vecinos del nodo en la topologia.

    Reduce el trafico entre hosts poniendo VMs conectadas en el mismo worker.
    El puntaje es la proporcion de vecinos ya asignados que estan en este host.
    """
    nombre = "afinidad_topologica"

    def __init__(self, peso: float = 1.0):
        self.peso = peso

    def puntuar(self, host: Host, nodo: Nodo, contexto: dict[str, Any]) -> float:
        slice_obj: Slice | None = contexto.get("slice")
        asignacion_parcial: dict[str, str] = contexto.get("asignacion_parcial", {})
        if slice_obj is None:
            return 0.0

        # Vecinos de este nodo en la topologia. El Scheduler los precalcula
        # una sola vez por slice (contexto["vecinos"]); recorrer todos los
        # enlaces en cada evaluacion es O(enlaces) por host y por nodo, lo que
        # pesa en slices grandes (una malla de n nodos tiene n(n-1)/2 enlaces).
        mapa_vecinos = contexto.get("vecinos")
        if mapa_vecinos is not None:
            vecinos = mapa_vecinos.get(nodo.id, set())
        else:
            vecinos = _vecinos_por_nodo(slice_obj).get(nodo.id, set())

        if not vecinos:
            return 0.0

        # Cuantos vecinos ya estan asignados a este host?
        en_este_host = sum(
            1 for v in vecinos
            if asignacion_parcial.get(v) == host.id
        )
        return en_este_host / len(vecinos)


def _vecinos_por_nodo(slice_obj: Slice) -> dict[str, set]:
    """nodo_id -> conjunto de nodo_id vecinos (una sola pasada por los enlaces)."""
    vecinos: dict[str, set] = {}
    for enlace in slice_obj.enlaces:
        vecinos.setdefault(enlace.nodo_origen, set()).add(enlace.nodo_destino)
        vecinos.setdefault(enlace.nodo_destino, set()).add(enlace.nodo_origen)
    return vecinos


class PesadorAntiAfinidad(Pesador):
    """Prefiere hosts con menos VMs del slice (distribuir para tolerancia a fallos)."""
    nombre = "anti_afinidad"

    def __init__(self, peso: float = 1.0):
        self.peso = peso

    def puntuar(self, host: Host, nodo: Nodo, contexto: dict[str, Any]) -> float:
        asignacion_parcial: dict[str, str] = contexto.get("asignacion_parcial", {})
        total_hosts = contexto.get("total_hosts", 1)
        en_este_host = sum(1 for h in asignacion_parcial.values() if h == host.id)
        total_asignados = len(asignacion_parcial)
        if total_asignados == 0:
            return 1.0
        # Menos VMs en este host = mejor puntaje
        promedio = total_asignados / max(total_hosts, 1)
        if promedio <= 0:
            return 1.0
        return max(0.0, 1.0 - (en_este_host / (promedio * 2)))


class PesadorEnergia(Pesador):
    """Prefiere hosts ya encendidos y más llenos: consolida VMs (best-fit).

    Objetivo de energía de R4: cuantos menos servidores con carga, más se
    pueden apagar o dejar en reposo. Puntaje en [0, 1]:

        0.5 * (el host ya tiene VMs) + 0.5 * (utilización de vCPU tras colocar el nodo)

    Como el scheduler descuenta cada nodo en la copia del host antes de
    decidir el siguiente, un host recién encendido por este slice pasa a
    contar como encendido para los nodos que siguen. Nunca viola capacidad:
    los filtros (incluida la congestión futura) se aplican antes.
    """
    nombre = "energia"

    def __init__(self, peso: float = 1.0):
        self.peso = peso

    def puntuar(self, host: Host, nodo: Nodo, contexto: dict[str, Any]) -> float:
        if host.vcpu_efectivos <= 0:
            return 0.0
        encendido = 1.0 if (host.vcpu_usados > 0 or host.ram_usados_mb > 0) else 0.0
        llenado = min(1.0, (host.vcpu_usados + nodo.vcpu) / host.vcpu_efectivos)
        return 0.5 * encendido + 0.5 * llenado


# Perfiles preconfigurados de pesadores
PERFIL_BALANCEADO: list[Pesador] = [
    PesadorBalanceVCPU(peso=1.0),
    PesadorBalanceRAM(peso=0.8),
]

PERFIL_AFINIDAD_TOPOLOGICA: list[Pesador] = [
    PesadorBalanceVCPU(peso=0.6),
    PesadorBalanceRAM(peso=0.4),
    # Hallazgo 13-nov-2026 (estado-proyecto Sec9octies): peso bajado de 1.5 a 0.3
    # para que la afinidad topologica NUNCA gane sobre el balance de recursos
    # (0.6+0.4=1.0). Antes apinaba VMs conectadas en un solo host hasta
    # quedarse sin disco real, y forzabamos ese host a Nova -> NoValidHost.
    PesadorAfinidadTopologica(peso=0.3),
    # Segundo hallazgo (mismo dia, probado contra el cluster real): bajar
    # el peso de afinidad NO basta para un arbol tipo "estrella" (un nodo
    # central con varias hojas). Cada hoja nueva tiene UN solo vecino (el
    # nodo central), asi que su bono de afinidad siempre es el maximo
    # (1.0) hacia el host de ese nodo central -- y como un host recien
    # usado por 1-2 VMs chicas apenas se distingue de uno vacio en balance
    # de vCPU/RAM, ese bono sigue bastando para seguir apilando ahi. Se
    # agrega anti-afinidad (ya existia para PERFIL_DISTRIBUIDO) para que
    # penalice a un host que ya tiene mas VMs de este slice que el
    # promedio, contrarrestando el apilamiento en topologias tipo
    # arbol/estrella sin eliminar del todo la preferencia por cercania
    # topologica.
    PesadorAntiAfinidad(peso=0.5),
]

PERFIL_DISTRIBUIDO: list[Pesador] = [
    PesadorBalanceVCPU(peso=0.5),
    PesadorBalanceRAM(peso=0.3),
    PesadorAntiAfinidad(peso=2.0),
]

# Ahorro de energía: consolidar en pocos hosts, manteniendo la afinidad
# topológica para que los vecinos del slice queden juntos.
PERFIL_ENERGIA: list[Pesador] = [
    PesadorEnergia(peso=1.0),
    PesadorAfinidadTopologica(peso=0.3),
]

# Objetivo del scheduler -> perfil de pesadores. "balance" es el de siempre.
PERFILES_POR_OBJETIVO: dict[str, list[Pesador]] = {
    "balance": PERFIL_AFINIDAD_TOPOLOGICA,
    "energia": PERFIL_ENERGIA,
    "distribuido": PERFIL_DISTRIBUIDO,
}


def perfil_por_objetivo(objetivo: str | None = None) -> tuple[str, list[Pesador]]:
    """Devuelve (objetivo, pesadores). Sin objetivo explícito se lee
    ORQ_SCHEDULER_OBJETIVO; si tampoco hay, "balance"."""
    nombre = (objetivo or os.environ.get("ORQ_SCHEDULER_OBJETIVO") or "balance").strip().lower()
    if nombre not in PERFILES_POR_OBJETIVO:
        raise ValueError(f"Objetivo de scheduler desconocido: {nombre!r} "
                         f"(use {', '.join(sorted(PERFILES_POR_OBJETIVO))})")
    return nombre, list(PERFILES_POR_OBJETIVO[nombre])


# ============================== SCHEDULER ====================================

class ConfigScheduler:
    """Configuracion del scheduler.

    Parametros nuevos (congestion futura y limite de tiempo); si se dejan en
    None se leen de variables de entorno:

        modelo_congestion  ModeloCongestion para FiltroCongestionFutura
                           (ORQ_CONGESTION_*). False lo desactiva.
        tiempo_limite_s    segundos maximos de calculo con pesadores antes de
                           pasar a modo rapido (ORQ_SCHEDULER_TIEMPO_LIMITE,
                           por defecto 5 s). 0 o negativo = sin limite.
        reservas           RegistroReservas (por defecto el del proceso).
        objetivo           "balance" (por defecto), "energia" o "distribuido"
                           (ORQ_SCHEDULER_OBJETIVO); elige el perfil de
                           pesadores si no se pasan pesadores explícitos.
    """
    def __init__(
        self,
        filtros: list[Filtro] | None = None,
        pesadores: list[Pesador] | None = None,
        overcommit_vcpu: float = 1.0,
        overcommit_ram: float = 1.0,
        monitorear: bool = True,
        staleness_s: float = 60.0,
        modelo_congestion: ModeloCongestion | bool | None = None,
        tiempo_limite_s: float | None = None,
        reservas: RegistroReservas | None = None,
        objetivo: str | None = None,
    ):
        self.filtros = filtros if filtros is not None else list(FILTROS_POR_DEFECTO)
        if pesadores is not None:
            self.objetivo = objetivo or "personalizado"
            self.pesadores = pesadores
        else:
            self.objetivo, self.pesadores = perfil_por_objetivo(objetivo)
        self.overcommit_vcpu = overcommit_vcpu
        self.overcommit_ram = overcommit_ram
        self.monitorear = monitorear
        self.staleness_s = staleness_s
        if modelo_congestion is None or modelo_congestion is True:
            modelo_congestion = ModeloCongestion.desde_entorno()
        self.modelo_congestion: ModeloCongestion | None = modelo_congestion or None
        if tiempo_limite_s is None:
            tiempo_limite_s = _float_entorno("ORQ_SCHEDULER_TIEMPO_LIMITE", 5.0)
        self.tiempo_limite_s: float | None = tiempo_limite_s if tiempo_limite_s > 0 else None
        self.reservas = reservas if reservas is not None else REGISTRO_RESERVAS


class Scheduler:
    """Scheduler inteligente que decide donde va cada VM.

    Uso tipico::

        sched = Scheduler(driver=mi_driver)
        resultado = sched.programar(slc, hosts)
        # resultado.asignacion: {nodo_id: host_id, ...}
        # resultado.explicacion_texto(): string con la traza

    Compatible con vm_placement.asignar_hosts(): se puede usar como
    drop-in replacement.
    """

    def __init__(
        self,
        driver: ProveedorEstado | None = None,
        config: ConfigScheduler | None = None,
        reloj: Callable[[], float] = time.monotonic,
    ):
        self.config = config or ConfigScheduler()
        self.monitor = ResourceMonitor(
            driver=driver if self.config.monitorear else None,
            staleness_s=self.config.staleness_s,
            reservas=self.config.reservas,
        )
        self._reloj = reloj
        self._ultimo_resultado: ResultadoScheduler | None = None

    @property
    def ultimo_resultado(self) -> ResultadoScheduler | None:
        return self._ultimo_resultado

    def programar(
        self,
        slc: Slice,
        hosts: list[Host],
        forzar_refresco: bool = False,
    ) -> ResultadoScheduler:
        """Decide el host de cada nodo del slice.

        Trabaja sobre copias de los hosts para no mutar el inventario real
        hasta que el llamador confirme la asignacion (igual que el viejo
        vm_placement.asignar_hosts).

        Si el calculo supera ``config.tiempo_limite_s``, los nodos restantes
        se asignan en modo rapido: se aplican los mismos filtros (nunca se
        viola capacidad, zona ni congestion) pero, en vez de los pesadores,
        se elige el host con mas vCPU libre. Asi un slice muy grande no
        bloquea la cola de despliegues.
        """
        inicio = self._reloj()
        limite = self.config.tiempo_limite_s

        # Aplicar overcommit a los hosts
        copias = []
        for h in hosts:
            copia = Host(**{**h.__dict__})
            copia.overcommit_vcpu = self.config.overcommit_vcpu
            copia.overcommit_ram = self.config.overcommit_ram
            copias.append(copia)

        # 1) Monitor: refrescar el estado real (+ reservas aun no medidas)
        self.monitor.refrescar(copias, forzar=forzar_refresco)

        # 2) Contexto compartido para filtros y pesadores
        contexto: dict[str, Any] = {
            "zona_disponibilidad": slc.zona_disponibilidad,
            "slice": slc,
            "asignacion_parcial": {},
            "total_hosts": len(copias),
            "vecinos": _vecinos_por_nodo(slc),
            "modelo_congestion": self.config.modelo_congestion,
        }
        suma_pesos = sum(p.peso for p in self.config.pesadores) or 1.0

        asignacion: dict[str, str] = {}
        decisiones: list[DecisionVM] = []
        modo_rapido: list[str] = []
        rapido = False

        # 3) Para cada nodo, filtrar -> pesar -> elegir
        for nodo in slc.nodos:
            if (not rapido and limite is not None
                    and self._reloj() - inicio > limite):
                rapido = True
                logger.warning(
                    "Scheduler: limite de %.1f s superado en el slice %s; "
                    "%d nodo(s) restantes van en modo rapido",
                    limite, slc.id, len(slc.nodos) - len(asignacion))
            if rapido:
                modo_rapido.append(nodo.id)

            decision = DecisionVM(nodo_id=nodo.id, nodo_nombre=nodo.nombre)
            mejor_host: Host | None = None
            mejor_puntaje: float = -1.0

            for copia in copias:
                candidato = CandidatoEvaluado(host_id=copia.id, paso_filtros=True)

                # Filtros (siempre, tambien en modo rapido)
                for filtro in self.config.filtros:
                    if not filtro.aceptar(copia, nodo, contexto):
                        candidato.paso_filtros = False
                        candidato.filtros_rechazados.append(filtro.nombre)

                if not candidato.paso_filtros:
                    decision.candidatos.append(candidato)
                    continue

                if rapido:
                    # Sin pesadores: el host con mas vCPU libre (proporcional)
                    total = (copia.vcpu_disponibles_con_overcommit / copia.vcpu_efectivos
                             if copia.vcpu_efectivos > 0 else 0.0)
                    candidato.puntaje_total = round(total, 4)
                else:
                    total = 0.0
                    for pesador in self.config.pesadores:
                        score = pesador.puntuar(copia, nodo, contexto)
                        ponderado = score * pesador.peso
                        candidato.puntajes[pesador.nombre] = round(ponderado, 4)
                        total += ponderado
                    candidato.puntaje_total = round(total / suma_pesos, 4)
                decision.candidatos.append(candidato)

                if candidato.puntaje_total > mejor_puntaje:
                    mejor_puntaje = candidato.puntaje_total
                    mejor_host = copia

            if mejor_host is None:
                motivos = sorted({r for c in decision.candidatos for r in c.filtros_rechazados})
                raise ValueError(
                    f"No hay host con capacidad suficiente para el nodo "
                    f"'{nodo.nombre}' (requiere {nodo.vcpu} vCPU / "
                    f"{nodo.ram_mb} MB RAM / {nodo.disco_gb} GB disco)"
                    + (f"; filtros que lo impiden: {', '.join(motivos)}" if motivos else "")
                )

            # Reservar en la copia (para que los nodos siguientes vean la capacidad reducida)
            mejor_host.vcpu_usados += nodo.vcpu
            mejor_host.ram_usados_mb += nodo.ram_mb
            mejor_host.disco_usado_gb += nodo.disco_gb

            asignacion[nodo.id] = mejor_host.id
            contexto["asignacion_parcial"] = dict(asignacion)
            decision.host_elegido = mejor_host.id
            decision.razon = (
                f"Puntaje {mejor_puntaje:.2f} "
                f"(vcpu_libre={mejor_host.vcpu_disponibles_con_overcommit:.0f}, "
                f"ram_libre={mejor_host.ram_disponible_con_overcommit_mb:.0f} MB"
            )
            modelo = self.config.modelo_congestion
            if modelo is not None:
                f_vcpu, f_ram = modelo.congestion_prevista(mejor_host)
                decision.razon += (
                    f", uso previsto al {100 * (1 - modelo.prob_desborde):.0f}%: "
                    f"vcpu {100 * f_vcpu:.0f}%, ram {100 * f_ram:.0f}%")
            decision.razon += ")"
            if rapido:
                decision.razon = "Modo rapido por limite de tiempo. " + decision.razon
            decisiones.append(decision)

        resultado = ResultadoScheduler(
            asignacion=asignacion,
            decisiones=decisiones,
            timestamp=time.time(),
            duracion_s=self._reloj() - inicio,
            nodos_modo_rapido=modo_rapido,
        )
        self._ultimo_resultado = resultado
        logger.info("Scheduler: %d nodos asignados a %d hosts distintos en %.3f s",
                     len(asignacion), len(set(asignacion.values())), resultado.duracion_s)
        return resultado
