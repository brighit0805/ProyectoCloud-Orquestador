"""Pruebas de reglas de seguridad Linux — iptables (R5 EX2, 15 pts).

Cubre las 4 reglas implementadas en network_manager:
  1. Aislamiento entre slices (chain ORQ-SL-<vlan>)
  2. Allowlist de puertos TCP
  3. Anti-spoofing IP/MAC
  4. Rate limiting de broadcasts

Los tests usan subprocess mock para no necesitar SSH real.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from slice_orchestrator import network_manager as nm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_ssh_ok(stdout: str = "") -> MagicMock:
    """Simula un subprocess.run exitoso."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = stdout
    m.stderr = ""
    return m


def _mock_ssh_fail(stderr: str = "error") -> MagicMock:
    """Simula un subprocess.run fallido."""
    m = MagicMock()
    m.returncode = 1
    m.stdout = ""
    m.stderr = stderr
    return m


# ---------------------------------------------------------------------------
# Tests — instalar_reglas_seguridad
# ---------------------------------------------------------------------------

class TestInstalarReglasSeguridad:

    def test_crea_chain_si_no_existe(self):
        """Si la chain ORQ-SL-<vlan> no existe, se crea con -N."""
        calls_hechos: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls_hechos.append(cmd)
            # _chain_existe usa -L → falla (chain no existe)
            if "-L" in cmd:
                return _mock_ssh_fail()
            # -C FORWARD → falla (no anclada aún)
            if "-C" in cmd and "FORWARD" in cmd:
                return _mock_ssh_fail()
            return _mock_ssh_ok()

        with patch("subprocess.run", side_effect=fake_run):
            nm.instalar_reglas_seguridad("192.168.201.1", vlan_id=3001)

        # Debe haber llamado a -N ORQ-SL-3001
        cmds = [" ".join(c) for c in calls_hechos]
        assert any("-N" in c and "ORQ-SL-3001" in c for c in cmds), (
            "Debe crear la chain con -N ORQ-SL-3001"
        )

    def test_no_recrea_chain_si_ya_existe(self):
        """Si la chain ya existe, NO vuelve a ejecutar -N."""
        calls_hechos: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls_hechos.append(cmd)
            if "-L" in cmd:
                return _mock_ssh_ok()   # chain existe
            if "-C" in cmd and "FORWARD" in cmd:
                return _mock_ssh_ok()   # ya anclada
            return _mock_ssh_ok()

        with patch("subprocess.run", side_effect=fake_run):
            nm.instalar_reglas_seguridad("192.168.201.1", vlan_id=3001)

        cmds = [" ".join(c) for c in calls_hechos]
        assert not any("-N" in c and "ORQ-SL-3001" in c for c in cmds), (
            "No debe recrear la chain si ya existe"
        )

    def test_regla1_aislamiento_entre_slices(self):
        """Debe agregar reglas ACCEPT intra-VLAN y DROP inter-VLAN."""
        cmds_iptables: list[str] = []

        def fake_run(cmd, **kwargs):
            if "iptables" in " ".join(cmd):
                cmds_iptables.append(" ".join(cmd))
            if "-L" in cmd:
                return _mock_ssh_fail()
            if "-C" in cmd:
                return _mock_ssh_fail()
            return _mock_ssh_ok()

        with patch("subprocess.run", side_effect=fake_run):
            nm.instalar_reglas_seguridad("192.168.201.1", vlan_id=3002)

        assert any("ACCEPT" in c and "intra" in c for c in cmds_iptables), (
            "Debe haber regla ACCEPT para tráfico intra-slice"
        )
        assert any("DROP" in c and "inter" in c for c in cmds_iptables), (
            "Debe haber regla DROP para tráfico inter-slice"
        )

    def test_regla2_allowlist_puertos(self):
        """SSH (22) debe estar en la allowlist; otros TCP bloqueados."""
        cmds_iptables: list[str] = []

        def fake_run(cmd, **kwargs):
            if "iptables" in " ".join(cmd):
                cmds_iptables.append(" ".join(cmd))
            if "-L" in cmd:
                return _mock_ssh_fail()
            if "-C" in cmd:
                return _mock_ssh_fail()
            return _mock_ssh_ok()

        with patch("subprocess.run", side_effect=fake_run):
            nm.instalar_reglas_seguridad(
                "192.168.201.1", vlan_id=3003,
                puertos_extra=[80, 443]
            )

        assert any("--dport 22" in c and "ACCEPT" in c for c in cmds_iptables), (
            "Puerto 22 (SSH) debe estar en la allowlist"
        )
        assert any("--dport 80" in c and "ACCEPT" in c for c in cmds_iptables), (
            "Puerto 80 debe estar en la allowlist (puertos_extra)"
        )
        assert any("tcp-block" in c and "DROP" in c for c in cmds_iptables), (
            "Debe haber regla DROP para TCP no permitido"
        )

    def test_regla3_antispoofing_ip(self):
        """Con vm_ip, debe agregar regla de anti-spoofing por IP."""
        cmds_iptables: list[str] = []

        def fake_run(cmd, **kwargs):
            if "iptables" in " ".join(cmd):
                cmds_iptables.append(" ".join(cmd))
            if "-L" in cmd:
                return _mock_ssh_fail()
            if "-C" in cmd:
                return _mock_ssh_fail()
            return _mock_ssh_ok()

        with patch("subprocess.run", side_effect=fake_run):
            nm.instalar_reglas_seguridad(
                "192.168.201.1", vlan_id=3004,
                vm_ip="192.168.122.100"
            )

        assert any("antispoofing-ip" in c and "DROP" in c for c in cmds_iptables), (
            "Debe haber regla anti-spoofing de IP"
        )

    def test_regla3_antispoofing_no_agrega_si_no_hay_ip(self):
        """Sin vm_ip, NO debe agregar regla anti-spoofing."""
        cmds_iptables: list[str] = []

        def fake_run(cmd, **kwargs):
            if "iptables" in " ".join(cmd):
                cmds_iptables.append(" ".join(cmd))
            if "-L" in cmd:
                return _mock_ssh_fail()
            if "-C" in cmd:
                return _mock_ssh_fail()
            return _mock_ssh_ok()

        with patch("subprocess.run", side_effect=fake_run):
            nm.instalar_reglas_seguridad("192.168.201.1", vlan_id=3005)

        assert not any("antispoofing-ip" in c for c in cmds_iptables), (
            "Sin vm_ip no debe agregar anti-spoofing"
        )

    def test_regla4_rate_limiting_broadcasts(self):
        """Debe agregar regla de rate limiting para broadcasts."""
        cmds_iptables: list[str] = []

        def fake_run(cmd, **kwargs):
            if "iptables" in " ".join(cmd):
                cmds_iptables.append(" ".join(cmd))
            if "-L" in cmd:
                return _mock_ssh_fail()
            if "-C" in cmd:
                return _mock_ssh_fail()
            return _mock_ssh_ok()

        with patch("subprocess.run", side_effect=fake_run):
            nm.instalar_reglas_seguridad("192.168.201.1", vlan_id=3006)

        assert any("bcast-limit-allow" in c and "255.255.255.255" in c
                   for c in cmds_iptables), (
            "Debe haber regla de rate limiting de broadcasts (ACCEPT con limit)"
        )
        assert any("bcast-limit-drop" in c and "DROP" in c
                   for c in cmds_iptables), (
            "Debe haber regla DROP para broadcasts que excedan el límite"
        )


# ---------------------------------------------------------------------------
# Tests — limpiar_reglas_seguridad
# ---------------------------------------------------------------------------

class TestLimpiarReglasSeguridad:

    def test_limpia_chain_existente(self):
        """Al eliminar un slice, debe hacer flush y delete de la chain."""
        cmds_iptables: list[str] = []

        def fake_run(cmd, **kwargs):
            if "iptables" in " ".join(cmd):
                cmds_iptables.append(" ".join(cmd))
            # chain existe
            if "-L" in cmd and "ORQ-SL-3001" in cmd:
                return _mock_ssh_ok()
            return _mock_ssh_ok()

        with patch("subprocess.run", side_effect=fake_run):
            nm.limpiar_reglas_seguridad("192.168.201.1", vlan_id=3001)

        assert any("-F" in c and "ORQ-SL-3001" in c for c in cmds_iptables), (
            "Debe hacer flush (-F) de la chain al limpiar"
        )
        assert any("-X" in c and "ORQ-SL-3001" in c for c in cmds_iptables), (
            "Debe borrar (-X) la chain al limpiar"
        )

    def test_limpiar_sin_chain_no_falla(self):
        """Si la chain no existe, limpiar no debe lanzar excepción."""
        def fake_run(cmd, **kwargs):
            if "-L" in cmd:
                return _mock_ssh_fail()   # chain no existe
            return _mock_ssh_ok()

        with patch("subprocess.run", side_effect=fake_run):
            # No debe lanzar excepción
            nm.limpiar_reglas_seguridad("192.168.201.1", vlan_id=9999)
