"""A session report must carry a «what I checked» section; scripts, the desk and silent notices are exempt."""
import json

import pytest

from polar import cli, db

SID = "0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"
GOOD = "## Qué hice\nArreglé X.\n\n## Qué revisé y qué no\nPruebas: 404 en verde. No probé en el teléfono.\n"


@pytest.fixture(autouse=True)
def _on(monkeypatch, tmp_path):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("POLAR_REPORT_EVIDENCE", raising=False)


@pytest.mark.parametrize("text", [
    GOOD,
    "### Qué comprobé\n- consulta en producción: 12 filas, igual que el CSV\n",
    "**Qué revisé:** las 3 pruebas nuevas pasan y la página carga en staging.\n",
    "# Pruebas\npytest: 410 passed\n## Qué falta\nnada",
    "## What I checked\nUnit tests green, staging page loads.\n",
    "## Qué verifiqué\nla consulta da 12 filas en producción\n",
    "## Qué probé\nel flujo completo en staging, sin errores\n",
    "## Qué revisé\n**Tests:**\n- 12 pasan en verde\n",
])
def test_has_evidence(text):
    assert not db.evidence_missing(text)


@pytest.mark.parametrize("text", [
    "## Qué hice\nArreglé X y quedó listo para producción.\n",
    "## Qué revisé\n\n## Qué falta\nnada\n",          # heading with nothing under it
    "Revisé todo y está bien.",                        # a word in the text, not a section
    "",
])
def test_missing(text):
    assert db.evidence_missing(text)


def _send(tmp_path, text, *extra, requester=SID + " task-x"):
    f = tmp_path / "r.md"
    f.write_text(text, encoding="utf-8")
    return cli.main(["report", "--title", "T", "--text-file", str(f), "--requester", requester, *extra])


def test_cli_refuses_session_report_without_section(tmp_path, capsys):
    assert _send(tmp_path, "## Qué hice\nListo, todo funciona bien ya.\n") != 0
    assert "Qué revisé" in capsys.readouterr().out


def test_cli_accepts_with_section(tmp_path, capsys):
    assert _send(tmp_path, GOOD) == 0
    assert json.loads(capsys.readouterr().out)["id"]


@pytest.mark.parametrize("who", ["watchdog", "release-desk-79", "unknown-session", ""])
def test_scripts_and_desk_are_exempt(tmp_path, capsys, who):
    assert _send(tmp_path, "Disco al 91 %.", requester=who) == 0


def test_silent_and_off_switch_are_exempt(tmp_path, capsys, monkeypatch):
    assert _send(tmp_path, "aviso", "--silent") == 0
    monkeypatch.setenv("POLAR_REPORT_EVIDENCE", "off")
    assert _send(tmp_path, "## Qué hice\nListo.") == 0


def test_prod_op_prefix_names_the_session():
    with pytest.raises(ValueError):
        db.check_report_evidence("Listo.", "prod-op op123 " + SID, False)
