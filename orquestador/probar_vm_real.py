from slice_orchestrator.driver_linux import DriverLinux, ModoDriver

URI = "qemu+ssh://ubuntu@10.0.10.2/system"

driver = DriverLinux(
    modo=ModoDriver.REAL,
    bridges_por_host={
        URI: "ovs1",
    },
)

print("Creando VM real de prueba...")

driver.crear_vm(
    host_uri=URI,
    nombre_vm="prueba-cirros",
    vcpu=1,
    ram_mb=512,
    disco_gb=1,
    imagen="cirros-0.6.2.qcow2",
    redes=[("red-prueba", 3000)],
)

print("VM prueba-cirros creada.")
