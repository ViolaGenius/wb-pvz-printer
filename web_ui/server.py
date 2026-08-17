"""
web_ui/server.py
Локальный Flask-сервер для окна настроек (отображается через pywebview, без
внешнего браузера — см. main.py). Сервер общается с работающим Engine
(engine.py), поэтому изменения из UI (пауза, шаблон, калибровка) сразу же
влияют на фоновый процесс автопечати без перезапуска программы.
"""

import io
import logging
import tempfile
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

import config as config_module
import print_log
import printer_tspl

log = logging.getLogger("web_ui")

STATIC_DIR = Path(__file__).parent / "static"


def create_app(engine) -> Flask:
    app = Flask(__name__, static_folder=None)

    # ---------- статика фронтенда ----------

    @app.route("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.route("/<path:filename>")
    def static_files(filename):
        return send_from_directory(STATIC_DIR, filename)

    # ---------- конфигурация ----------

    @app.route("/api/config", methods=["GET"])
    def get_config():
        return jsonify(engine.get_config())

    @app.route("/api/config", methods=["POST"])
    def patch_config():
        patch = request.get_json(force=True)
        cfg = engine.update_config(patch)
        return jsonify(cfg)

    # ---------- статус / общие действия ----------

    @app.route("/api/status", methods=["GET"])
    def get_status():
        return jsonify(engine.get_status_snapshot())

    @app.route("/api/pause", methods=["POST"])
    def toggle_pause():
        paused = engine.toggle_pause()
        return jsonify({"paused": paused})

    @app.route("/api/print-repeat", methods=["POST"])
    def print_repeat():
        ok, err = engine.repeat_last_print()
        return jsonify({"ok": ok, "error": err})

    # ---------- принтер ----------

    @app.route("/api/printers", methods=["GET"])
    def get_printers():
        return jsonify({"printers": printer_tspl.list_printers()})

    @app.route("/api/print-test", methods=["POST"])
    def print_test():
        data = request.get_json(force=True)
        cell_number = str(data.get("cell_number", "")).strip()
        if not cell_number:
            return jsonify({"ok": False, "error": "Не указан номер ячейки"}), 400
        ok, err = engine.test_print(cell_number)
        return jsonify({"ok": ok, "error": err})

    # ---------- калибровка / проверка области ----------

    @app.route("/api/calibrate/start", methods=["POST"])
    def calibrate_start():
        data = request.get_json(force=True, silent=True) or {}
        seconds = int(data.get("seconds", 4))
        result = engine.calibrate_start(seconds=seconds)
        return jsonify(result)

    @app.route("/api/calibrate/save", methods=["POST"])
    def calibrate_save():
        data = request.get_json(force=True)
        result = engine.calibrate_save(
            x=int(data["x"]), y=int(data["y"]), width=int(data["width"]), height=int(data["height"])
        )
        return jsonify(result)

    @app.route("/api/health-check", methods=["GET"])
    def health_check_endpoint():
        return jsonify(engine.run_health_checks())

    @app.route("/api/digit-templates", methods=["GET"])
    def digit_templates_status():
        return jsonify(engine.digit_templates_status())

    @app.route("/api/digit-templates/reset", methods=["POST"])
    def digit_templates_reset():
        return jsonify(engine.digit_templates_reset())

    @app.route("/api/test-region", methods=["POST"])
    def test_region():
        result = engine.test_region()
        return jsonify(result)

    @app.route("/api/test-scan", methods=["POST"])
    def test_scan():
        result = engine.test_scan()
        return jsonify(result)

    @app.route("/api/preview-regions", methods=["GET"])
    def preview_regions():
        result = engine.preview_regions()
        return jsonify(result)

    # ---------- цветовые точки-триггеры ----------

    @app.route("/api/color-triggers", methods=["POST"])
    def add_color_trigger():
        data = request.get_json(force=True)
        result = engine.color_trigger_add(
            x=int(data["x"]), y=int(data["y"]),
            color_rgb=[int(c) for c in data["color_rgb"]],
            tolerance_percent=float(data.get("tolerance_percent", 12)),
        )
        return jsonify(result)

    @app.route("/api/color-triggers/<trigger_id>", methods=["POST"])
    def update_color_trigger(trigger_id):
        patch = request.get_json(force=True)
        result = engine.color_trigger_update(trigger_id, patch)
        return jsonify(result)

    @app.route("/api/color-triggers/<trigger_id>", methods=["DELETE"])
    def delete_color_trigger(trigger_id):
        result = engine.color_trigger_delete(trigger_id)
        return jsonify(result)

    # ---------- привязка сканера к физическому устройству (см. raw_input_listener.py) ----------

    @app.route("/api/scanner-device/qr.png", methods=["GET"])
    def scanner_device_qr():
        """QR-код для калибровки — его СОДЕРЖИМОЕ не имеет значения (программа не
        декодирует сам QR, только смотрит, С КАКОГО устройства пришли нажатия),
        поэтому просто фиксированная строка, лишь бы сканер мог её прочитать."""
        import qrcode
        img = qrcode.make("WB_PVZ_SCANNER_CALIBRATION")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return Response(buf.getvalue(), mimetype="image/png")

    @app.route("/api/scanner-device/start-detect", methods=["POST"])
    def scanner_device_start_detect():
        ok, err = engine.scanner_device_detect_start()
        return jsonify({"ok": ok, "error": err})

    @app.route("/api/scanner-device/cancel-detect", methods=["POST"])
    def scanner_device_cancel_detect():
        engine.scanner_device_detect_cancel()
        return jsonify({"ok": True})

    @app.route("/api/scanner-device/poll", methods=["GET"])
    def scanner_device_poll():
        detected = engine.scanner_device_detect_poll()
        return jsonify({"ok": True, "detected": detected})

    @app.route("/api/scanner-device/bind", methods=["POST"])
    def scanner_device_bind():
        data = request.get_json(force=True)
        engine.scanner_device_bind(data["device_path"], data.get("device_name", ""))
        return jsonify({"ok": True})

    @app.route("/api/scanner-device/unbind", methods=["POST"])
    def scanner_device_unbind():
        engine.scanner_device_unbind()
        return jsonify({"ok": True})

    # ---------- журнал печати ----------

    @app.route("/api/log", methods=["GET"])
    def get_log():
        limit = int(request.args.get("limit", 200))
        status = request.args.get("status") or None
        search = request.args.get("search") or None
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")
        entries = print_log.get_recent(
            limit,
            date_from=float(date_from) if date_from else None,
            date_to=float(date_to) if date_to else None,
            status=status,
            search=search,
        )
        return jsonify({"entries": entries})

    @app.route("/api/log", methods=["DELETE"])
    def clear_log():
        print_log.clear_log()
        return jsonify({"ok": True})

    @app.route("/api/log/export.csv", methods=["GET"])
    def export_log_csv():
        status = request.args.get("status") or None
        search = request.args.get("search") or None
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")
        csv_text = print_log.export_csv(
            date_from=float(date_from) if date_from else None,
            date_to=float(date_to) if date_to else None,
            status=status,
            search=search,
        )
        from flask import Response
        # BOM в начале — чтобы Excel на Windows сразу правильно определил UTF-8
        return Response(
            "\ufeff" + csv_text,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=wb_pvz_log.csv"},
        )

    # ---------- шаблоны / картинки ----------

    @app.route("/api/templates/active", methods=["POST"])
    def set_active_template():
        data = request.get_json(force=True)
        name = data.get("name")
        cfg = engine.get_config()
        if name not in cfg["templates"]:
            return jsonify({"ok": False, "error": "Шаблон не найден"}), 404
        engine.update_config({"active_template": name})
        return jsonify({"ok": True})

    @app.route("/api/templates", methods=["POST"])
    def save_template():
        """Создаёт/обновляет именованный шаблон целиком (elements)."""
        data = request.get_json(force=True)
        name = data.get("name")
        elements = data.get("elements")
        if not name or not elements:
            return jsonify({"ok": False, "error": "Нужны name и elements"}), 400
        cfg = engine.get_config()
        templates = dict(cfg.get("templates", {}))
        templates[name] = {"elements": elements}
        engine.update_config({"templates": templates})
        return jsonify({"ok": True})

    @app.route("/api/templates/<name>", methods=["DELETE"])
    def delete_template(name):
        cfg = engine.get_config()
        templates = dict(cfg.get("templates", {}))
        if name == "default":
            return jsonify({"ok": False, "error": "Нельзя удалить шаблон по умолчанию"}), 400
        if name in templates:
            del templates[name]
            patch = {"templates": templates}
            if cfg.get("active_template") == name:
                patch["active_template"] = "default"
            engine.update_config(patch)
        return jsonify({"ok": True})

    @app.route("/api/template-preview", methods=["POST"])
    def template_preview():
        data = request.get_json(force=True)
        elements = data.get("elements") or {}
        cell_number = str(data.get("cell_number", "") or "")
        result = engine.render_template_preview(elements, cell_number)
        return jsonify(result)

    @app.route("/api/upload-image", methods=["POST"])
    def upload_image():
        if "file" not in request.files:
            return jsonify({"ok": False, "error": "Файл не передан"}), 400
        f = request.files["file"]
        dest_dir = config_module.get_app_data_dir() / "images"
        dest_dir.mkdir(exist_ok=True)
        safe_name = Path(f.filename).name
        dest_path = dest_dir / safe_name
        f.save(dest_path)
        return jsonify({"ok": True, "path": str(dest_path)})

    @app.route("/api/uploaded-image/<path:filename>", methods=["GET"])
    def get_uploaded_image(filename):
        images_dir = config_module.get_app_data_dir() / "images"
        return send_from_directory(images_dir, filename)

    # ---------- пользовательские звуки (капча/успех/ошибка) ----------

    ALLOWED_SOUND_KINDS = {"capture", "success", "error"}

    @app.route("/api/upload-sound", methods=["POST"])
    def upload_sound():
        import sound as sound_module
        kind = request.form.get("kind", "")
        if kind not in ALLOWED_SOUND_KINDS:
            return jsonify({"ok": False, "error": "Неизвестный тип звука"}), 400
        if "file" not in request.files:
            return jsonify({"ok": False, "error": "Файл не передан"}), 400
        f = request.files["file"]
        header = f.read(12)
        f.seek(0)
        # простая проверка WAV-контейнера (RIFF....WAVE) — winsound.PlaySound
        # умеет проигрывать только WAV, другие форматы (mp3 и т.п.) молча не
        # заиграют, лучше сообщить об этом сразу при загрузке, а не в момент скана
        if not (header[0:4] == b"RIFF" and header[8:12] == b"WAVE"):
            return jsonify({
                "ok": False,
                "error": "Это не WAV-файл. Windows-звук (winsound) умеет проигрывать только .wav — "
                         "сконвертируйте файл (например, через любой бесплатный онлайн-конвертер или "
                         "VLC: Медиа → Конвертировать) и загрузите заново.",
            }), 400
        dest_dir = sound_module.get_user_sounds_dir()
        dest_path = dest_dir / f"{kind}.wav"
        f.save(dest_path)
        return jsonify({"ok": True})

    @app.route("/api/sound/<kind>", methods=["GET"])
    def get_sound(kind):
        import sound as sound_module
        if kind not in ALLOWED_SOUND_KINDS:
            return jsonify({"ok": False, "error": "Неизвестный тип звука"}), 400
        wav_path = sound_module.resolve_wav_path(f"{kind}.wav")
        if wav_path is None:
            return jsonify({"ok": False, "error": "Звук не задан (используется системный сигнал)"}), 404
        return send_from_directory(wav_path.parent, wav_path.name)

    @app.route("/api/sound/<kind>", methods=["DELETE"])
    def delete_custom_sound(kind):
        import sound as sound_module
        if kind not in ALLOWED_SOUND_KINDS:
            return jsonify({"ok": False, "error": "Неизвестный тип звука"}), 400
        path = sound_module.get_user_sounds_dir() / f"{kind}.wav"
        if path.exists():
            path.unlink()
        return jsonify({"ok": True})

    # ---------- экспорт / импорт настроек ----------

    @app.route("/api/export", methods=["GET"])
    def export_config():
        tmp = Path(tempfile.gettempdir()) / "wb_pvz_config_export.json"
        config_module.export_config(str(tmp))
        return send_from_directory(tmp.parent, tmp.name, as_attachment=True, download_name="wb_pvz_config.json")

    @app.route("/api/import", methods=["POST"])
    def import_config():
        if "file" not in request.files:
            return jsonify({"ok": False, "error": "Файл не передан"}), 400
        f = request.files["file"]
        tmp = Path(tempfile.gettempdir()) / "wb_pvz_config_import.json"
        f.save(tmp)
        try:
            cfg = config_module.import_config(str(tmp))
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)}), 400
        engine.reload_config()
        return jsonify({"ok": True, "config": cfg})

    return app


def run_server(engine, host="127.0.0.1", port=8765):
    app = create_app(engine)
    # use_reloader=False обязателен — иначе Flask в debug-режиме попытается
    # перезапустить процесс вторым потоком и сломает уже запущенные хуки/трей
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
