# Entregable 3 - Código de despliegue de slices

Primera versión funcional del Grupo 2 para TEL141. Implementa, mediante
funciones Python y sin API HTTP, la creación y eliminación de slices con
topologías lineal y anillo sobre múltiples hosts Linux.

## Ejecución segura

```bash
python3 demo.py
python3 -m unittest discover -s tests -v
```

El modo predeterminado es **simulado**. La presencia de `libvirt-python` no
activa operaciones reales automáticamente. Para el laboratorio, el grupo debe
reemplazar las URI de ejemplo, confirmar el rango VLAN, preparar el bridge OVS
`br-provider` en los hosts y validar la imagen base en el pool de libvirt.

La versión entregada es compatible con Python 3.8 o superior, incluida la
instalación actual de Server 4.

## Módulos

- `models.py`: entidades `Slice`, `Nodo`, `Enlace` y `Host`.
- `topology_manager.py`: genera nodos, enlaces y orden de despliegue.
- `vm_placement.py`: elige el host físico y administra su capacidad.
- `network_manager.py`: reserva y libera VLAN sin duplicarlas.
- `driver_linux.py`: modo simulado explícito y modo real mediante libvirt/KVM.
- `orchestrator.py`: coordina creación, rollback y eliminación.
- `demo.py`: prueba manual reproducible.
- `tests/`: pruebas unitarias del flujo principal.

## Correcciones incorporadas

- Selección explícita entre modo simulado y real.
- VLAN únicas entre slices activos y liberación al eliminar.
- Liberación de vCPU y RAM después de borrar o revertir un slice.
- Rollback de cada recurso concreto, incluso ante fallos parciales por host.
- Creación y eliminación del volumen QCOW2 en el modo real.
- Etiquetado VLAN de las interfaces sobre un bridge Open vSwitch provider.
- Datos del laboratorio tratados como configuración pendiente de confirmar,
  no como direcciones reales asumidas.

## Alcance pendiente

Esta versión demuestra la lógica del requisito Linux y su comportamiento en
simulación. La prueba física multi-host requiere validar en el slice grupal:

1. URI y acceso SSH/libvirt de cada host.
2. Bridge OVS provider y enlace troncal entre hosts/OFS.
3. Rango VLAN asignado al Grupo 2.
4. Pool `default` e imagen base `generic-linux.qcow2`.
5. Conectividad entre VMs y aislamiento entre dos slices simultáneos.

Persistencia, cola de tareas, autenticación, monitoreo, API y OpenStack quedan
para iteraciones posteriores de la arquitectura.
