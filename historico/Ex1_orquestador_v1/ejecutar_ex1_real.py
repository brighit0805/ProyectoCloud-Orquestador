#!/usr/bin/env python3

from slice_orchestrator.driver_linux import DriverLinux, ModoDriver
from slice_orchestrator.models import Host
from slice_orchestrator.network_manager import VLANAllocator
from slice_orchestrator.orchestrator import desplegar_slice_existente
from slice_orchestrator.topology_manager import construir_ex1_topology
from slice_orchestrator.vm_placement import EstrategiaPlacement


URI1 = "qemu+ssh://ubuntu@10.0.10.1/system"
URI2 = "qemu+ssh://ubuntu@10.0.10.2/system"
URI3 = "qemu+ssh://ubuntu@10.0.10.3/system"


hosts = [
    Host("worker1", "worker1", URI1, 16, 32768, "az-lab"),
    Host("worker2", "worker2", URI2, 16, 32768, "az-lab"),
    Host("worker3", "worker3", URI3, 16, 32768, "az-lab"),
]


driver = DriverLinux(
    modo=ModoDriver.REAL,
    bridges_por_host={
        URI1: "br-int",
        URI2: "ovs1",
        URI3: "br-int",
    },
)

allocator = VLANAllocator(inicio=3000, fin=3099)


print("Construyendo EX1...")

slc = construir_ex1_topology(
    slice_id="slice-ex1",
    nombre="demo-ex1",
    owner="grupo2@pucp.edu.pe",
    zona_disponibilidad="az-lab",
)

# En la infraestructura real usamos numeracion VM1..VM6
# para que coincida directamente con el diagrama de validacion.
for i, nodo in enumerate(slc.nodos, 1):
    nodo.nombre = f"demo-ex1-vm{i}"

print("Desplegando EX1 en modo REAL...")

slc = desplegar_slice_existente(
    slc,
    hosts,
    driver,
    allocator,
    EstrategiaPlacement.ROUND_ROBIN,
)

print()
print("===== DESPLIEGUE COMPLETADO =====")

for i, nodo in enumerate(slc.nodos, 1):
    print(
        f"VM{i} -> {nodo.host_id} | "
        f"{nodo.imagen} | "
        f"{nodo.vcpu} vCPU | "
        f"{nodo.ram_mb} MB"
    )

print()
print("El slice queda ACTIVO para validacion.")
print("NO se elimina automaticamente.")
