#!/usr/bin/env python3
"""Demo del paso 6: reintento automático ante un fallo de despliegue (modo simulado).

Inyecta un fallo transitorio en una operación del driver; el orquestador reintenta
y el slice termina ACTIVO. Con --sin-reintentos se ve el rollback sin huérfanos.
"""
import sys

from slice_orchestrator.driver_linux import DriverLinux
from slice_orchestrator.models import Host
from slice_orchestrator.network_manager import VLANAllocator
from slice_orchestrator.orchestrator import DespliegueFallidoError, desplegar_slice_existente
from slice_orchestrator.topology_manager import construir_ex1_topology
from slice_orchestrator.vm_placement import EstrategiaPlacement

hosts = [Host(f"worker{i}", f"worker{i}", f"qemu+ssh://ubuntu@10.0.10.{i}/system", 16, 32768, "az-lab")
         for i in (1, 2, 3)]
reint = 1 if "--sin-reintentos" in sys.argv else 3
drv = DriverLinux(fallar_en_operacion=5)
slc = construir_ex1_topology("slice-ex1", "demo-ex1", "g2@pucp.edu.pe", "az-lab")
print(f"Se inyecta un fallo en la operación 5 del driver. Reintentos permitidos: {reint}")
try:
    s = desplegar_slice_existente(slc, hosts, drv, VLANAllocator(3000, 3099),
                                  EstrategiaPlacement.ROUND_ROBIN, reintentos=reint, espera_reintento_s=1.0)
    print(f"Resultado: slice {s.estado.value}; VMs creadas: {len(drv.vms_simuladas)}")
except DespliegueFallidoError as e:
    print(f"Despliegue fallido -> rollback. Recursos huérfanos: VMs={len(drv.vms_simuladas)}")
    print(f"Motivo: {e}")
