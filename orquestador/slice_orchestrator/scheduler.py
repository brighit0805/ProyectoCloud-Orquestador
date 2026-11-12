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

El scheduler NO ejecuta nada en la infraestructura: solo decide y devuelve
la asignacion. La ejecucion sigue en el orchestrator via el driver.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

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

    def explicacion_texto(self) -> str:
        """Resumen legible de todas las decisiones."""
        lineas = []
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
    """

    def __init__(self, driver: ProveedorEstado | None = None,
                 staleness_s: float = 60.0):
        self._driver = driver
        self._staleness_s = staleness_s
        self._ultimo_refresco: float = 0.0

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
                h.vcpu_usados = real.vcpu_usados
                h.ram_usados_mb = real.ram_usados_mb
                h.disco_usado_gb = real.disco_usado_gb
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


FILTROS_POR_DEFECTO: list[Filtro] = [
    FiltroZonaDisponibilidad(),
    FiltroHostActivo(),
    FiltroCapacidadVCPU(),
    FiltroCapacidadRAM(),
    FiltroCapacidadDisco(),
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

        # Encontrar vecinos de este nodo en la topologia
        vecinos = set()
        for enlace in slice_obj.enlaces:
            if enlace.nodo_origen == nodo.id:
                vecinos.add(enlace.nodo_destino)
            elif enlace.nodo_destino == nodo.id:
                vecinos.add(enlace.nodo_origen)

        if not vecinos:
            return 0.0

        # Cuantos vecinos ya estan asignados a este host?
        en_este_host = sum(
            1 for v in vecinos
            if asignacion_parcial.get(v) == host.id
        )
        return en_este_host / len(vecinos)


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


# Perfiles preconfigurados de pesadores
PERFIL_BALANCEADO: list[Pesador] = [
    PesadorBalanceVCPU(peso=1.0),
    PesadorBalanceRAM(peso=0.8),
]

PERFIL_AFINIDAD_TOPOLOGICA: list[Pesador] = [
    PesadorBalanceVCPU(peso=0.6),
    PesadorBalanceRAM(peso=0.4),
    PesadorAfinidadTopologica(peso=1.5),
]

PERFIL_DISTRIBUIDO: list[Pesador] = [
    PesadorBalanceVCPU(peso=0.5),
    PesadorBalanceRAM(peso=0.3),
    PesadorAntiAfinidad(peso=2.0),
]


# ============================== SCHEDULER ====================================

class ConfigScheduler:
    """Configuracion del scheduler."""
    def __init__(
        self,
        filtros: list[Filtro] | None = None,
        pesadores: list[Pesador] | None = None,
        overcommit_vcpu: float = 1.0,
        overcommit_ram: float = 1.0,
        monitorear: bool = True,
        staleness_s: float = 60.0,
    ):
        self.filtros = filtros if filtros is not None else list(FILTROS_POR_DEFECTO)
        self.pesadores = pesadores if pesadores is not None else list(PERFIL_AFINIDAD_TOPOLOGICA)
        self.overcommit_vcpu = overcommit_vcpu
        self.overcommit_ram = overcommit_ram
        self.monitorear = monitorear
        self.staleness_s = staleness_s


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
    ):
        self.config = config or ConfigScheduler()
        self.monitor = ResourceMonitor(
            driver=driver if self.config.monitorear else None,
            staleness_s=self.config.staleness_s,
        )
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
        """
        # Aplicar overcommit a los hosts
        copias = []
        for h in hosts:
            copia = Host(**{**h.__dict__})
            copia.overcommit_vcpu = self.config.overcommit_vcpu
            copia.overcommit_ram = self.config.overcommit_ram
            copias.append(copia)

        # 1) Monitor: refrescar el estado real
        self.monitor.refrescar(copias, forzar=forzar_refresco)

        # 2) Contexto compartido para filtros y pesadores
        contexto: dict[str, Any] = {
            "zona_disponibilidad": slc.zona_disponibilidad,
            "slice": slc,
            "asignacion_parcial": {},
            "total_hosts": len(copias),
        }

        asignacion: dict[str, str] = {}
        decisiones: list[DecisionVM] = []

        # 3) Para cada nodo, filtrar -> pesar -> elegir
        for nodo in slc.nodos:
            decision = DecisionVM(nodo_id=nodo.id, nodo_nombre=nodo.nombre)
            mejor_host: Host | None = None
            mejor_puntaje: float = -1.0

            for copia in copias:
                candidato = CandidatoEvaluado(host_id=copia.id, paso_filtros=True)

                # Filtros
                for filtro in self.config.filtros:
                    if not filtro.aceptar(copia, nodo, contexto):
                        candidato.paso_filtros = False
                        candidato.filtros_rechazados.append(filtro.nombre)

                if not candidato.paso_filtros:
                    decision.candidatos.append(candidato)
                    continue

                # Pesadores
                total = 0.0
                for pesador in self.config.pesadores:
                    score = pesador.puntuar(copia, nodo, contexto)
                    ponderado = score * pesador.peso
                    candidato.puntajes[pesador.nombre] = round(ponderado, 4)
                    total += ponderado

                suma_pesos = sum(p.peso for p in self.config.pesadores) or 1.0
                candidato.puntaje_total = round(total / suma_pesos, 4)
                decision.candidatos.append(candidato)

                if candidato.puntaje_total > mejor_puntaje:
                    mejor_puntaje = candidato.puntaje_total
                    mejor_host = copia

            if mejor_host is None:
                raise ValueError(
                    f"No hay host con capacidad suficiente para el nodo "
                    f"'{nodo.nombre}' (requiere {nodo.vcpu} vCPU / "
                    f"{nodo.ram_mb} MB RAM / {nodo.disco_gb} GB disco)"
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
                f"ram_libre={mejor_host.ram_disponible_con_overcommit_mb:.0f} MB)"
            )
            decisiones.append(decision)

        resultado = ResultadoScheduler(
            asignacion=asignacion,
            decisiones=decisiones,
            timestamp=time.time(),
        )
        self._ultimo_resultado = resultado
        logger.info("Scheduler: %d nodos asignados a %d hosts distintos",
                     len(asignacion), len(set(asignacion.values())))
        return resultado
