"""APK Builder – fragt die buildozer-Einstellungen ab, schickt den Code an
ein GitHub-Repo und lässt dort per GitHub Actions eine APK bauen."""

import base64
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from kivy.app import App
from kivy.clock import mainthread
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CTX = ssl.create_default_context()

API = "https://api.github.com"
WORKFLOW_NAME = "build-apk.yml"

# Workflow, der im Ziel-Repo die APK baut und als Release bereitstellt
WORKFLOW = r"""name: APK bauen

on:
  workflow_dispatch:

permissions:
  contents: write

jobs:
  build:
    runs-on: ubuntu-22.04
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - uses: actions/setup-java@v4
        with:
          distribution: temurin
          java-version: "17"

      - name: Abhaengigkeiten installieren
        run: |
          sudo apt-get update
          sudo apt-get install -y git zip unzip autoconf libtool pkg-config \
            zlib1g-dev libncurses5-dev libncursesw5-dev cmake libffi-dev \
            libssl-dev automake
          pip install --upgrade pip
          pip install buildozer "cython<3"

      - name: Buildozer-Cache
        uses: actions/cache@v4
        with:
          path: |
            ~/.buildozer
            .buildozer
          key: buildozer-${{ hashFiles('buildozer.spec') }}

      - name: APK bauen
        run: yes | buildozer -v android debug

      - name: Release erstellen
        uses: softprops/action-gh-release@v2
        with:
          tag_name: build-${{ github.run_number }}
          name: Build ${{ github.run_number }}
          files: bin/*.apk
"""

# (Schlüssel, Beschriftung, Standardwert, Hinweis)
FELDER = [
    ("token", "GitHub-Token", "", "ghp_… (Rechte: repo + workflow)"),
    ("repo", "Repository", "", "benutzer/repo-name"),
    ("title", "App-Name", "Meine App", "Name unter dem App-Symbol"),
    ("package", "Paketname", "meineapp", "nur kleine Buchstaben/Zahlen/_"),
    ("domain", "Domain", "org.example", "z.B. org.deinname"),
    ("version", "Version", "0.1", "z.B. 1.0"),
    ("requirements", "Pakete (requirements)", "python3,kivy", "mit Komma getrennt"),
    ("permissions", "Berechtigungen", "INTERNET", "z.B. INTERNET,CAMERA,VIBRATE"),
]


def make_spec(c):
    perms = ", ".join(p.strip().upper() for p in c["permissions"].split(",") if p.strip())
    reqs = ",".join(r.strip() for r in c["requirements"].split(",") if r.strip())
    return f"""[app]
title = {c['title'].strip()}
package.name = {c['package'].strip()}
package.domain = {c['domain'].strip()}
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json,txt,ttf,wav,mp3,ogg
version = {c['version'].strip()}
requirements = {reqs or 'python3,kivy'}
orientation = {c['orientation']}
fullscreen = {1 if c['fullscreen'] == 'Ja' else 0}
android.permissions = {perms or 'INTERNET'}
android.api = 34
android.minapi = 21
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 0
"""


def open_url(url):
    try:
        from jnius import autoclass
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        Intent = autoclass("android.content.Intent")
        Uri = autoclass("android.net.Uri")
        activity.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
    except Exception:
        webbrowser.open(url)


class GitHub:
    def __init__(self, token, repo):
        self.token = token.strip()
        self.repo = repo.strip().strip("/")

    def request(self, method, path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(API + path, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "APK-Builder-App")
        if body:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=SSL_CTX, timeout=30) as r:
                text = r.read().decode()
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="ignore")
            try:
                msg = json.loads(msg).get("message", msg)
            except Exception:
                pass
            raise RuntimeError(f"GitHub-Fehler {e.code}: {msg[:200]}")

    def put_file(self, path, text, message):
        sha = None
        try:
            sha = self.request("GET", f"/repos/{self.repo}/contents/{path}").get("sha")
        except RuntimeError as e:
            if "404" not in str(e):
                raise
        data = {"message": message,
                "content": base64.b64encode(text.encode("utf-8")).decode()}
        if sha:
            data["sha"] = sha
        self.request("PUT", f"/repos/{self.repo}/contents/{path}", data)

    def runs(self):
        try:
            res = self.request(
                "GET", f"/repos/{self.repo}/actions/workflows/{WORKFLOW_NAME}/runs?per_page=10")
            return res.get("workflow_runs", [])
        except RuntimeError as e:
            if "404" in str(e):
                return []
            raise


class Oberflaeche(BoxLayout):
    def __init__(self, app, **kw):
        super().__init__(orientation="vertical", padding=dp(12), spacing=dp(8), **kw)
        self.app = app
        self.inputs = {}
        self.run_url = None
        self.apk_url = None

        scroll = ScrollView()
        grid = GridLayout(cols=1, spacing=dp(6), size_hint_y=None)
        grid.bind(minimum_height=grid.setter("height"))

        def label(text):
            lbl = Label(text=text, size_hint_y=None, height=dp(26), halign="left", valign="middle")
            lbl.bind(size=lambda l, s: setattr(l, "text_size", s))
            return lbl

        gespeichert = app.load_settings()
        for key, titel, default, hint in FELDER:
            grid.add_widget(label(titel))
            ti = TextInput(text=gespeichert.get(key, default), hint_text=hint,
                           multiline=False, size_hint_y=None, height=dp(44),
                           password=(key == "token"))
            self.inputs[key] = ti
            grid.add_widget(ti)

        grid.add_widget(label("Ausrichtung"))
        self.orientation_sp = Spinner(text=gespeichert.get("orientation", "portrait"),
                                      values=("portrait", "landscape", "all"),
                                      size_hint_y=None, height=dp(44))
        grid.add_widget(self.orientation_sp)

        grid.add_widget(label("Vollbild"))
        self.fullscreen_sp = Spinner(text=gespeichert.get("fullscreen", "Nein"),
                                     values=("Nein", "Ja"), size_hint_y=None, height=dp(44))
        grid.add_widget(self.fullscreen_sp)

        grid.add_widget(label("main.py (Code einfügen – leer = im Repo lassen)"))
        self.code = TextInput(text=gespeichert.get("code", ""), multiline=True,
                              size_hint_y=None, height=dp(320),
                              hint_text="from kivy.app import App\n…")
        grid.add_widget(self.code)

        self.status_lbl = Label(text="Bereit.", size_hint_y=None, halign="left", valign="top")
        self.status_lbl.bind(width=lambda l, w: setattr(l, "text_size", (w, None)),
                             texture_size=lambda l, s: setattr(l, "height", s[1] + dp(10)))
        grid.add_widget(self.status_lbl)

        scroll.add_widget(grid)
        self.add_widget(scroll)

        reihe1 = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(8))
        speichern = Button(text="Speichern")
        speichern.bind(on_press=lambda *_: self.speichern())
        self.bau_btn = Button(text="APK bauen", bold=True)
        self.bau_btn.bind(on_press=lambda *_: self.bauen())
        reihe1.add_widget(speichern)
        reihe1.add_widget(self.bau_btn)
        self.add_widget(reihe1)

        reihe2 = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(8))
        self.log_btn = Button(text="Build ansehen", disabled=True)
        self.log_btn.bind(on_press=lambda *_: open_url(self.run_url))
        self.dl_btn = Button(text="APK herunterladen", disabled=True)
        self.dl_btn.bind(on_press=lambda *_: open_url(self.apk_url))
        reihe2.add_widget(self.log_btn)
        reihe2.add_widget(self.dl_btn)
        self.add_widget(reihe2)

    def werte(self):
        c = {k: ti.text for k, ti in self.inputs.items()}
        c["orientation"] = self.orientation_sp.text
        c["fullscreen"] = self.fullscreen_sp.text
        c["code"] = self.code.text
        return c

    def speichern(self):
        self.app.save_settings(self.werte())
        self.status("Einstellungen gespeichert.")

    def pruefen(self, c):
        if not c["token"].strip():
            return "Bitte GitHub-Token eingeben."
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", c["repo"].strip()):
            return "Repository bitte als benutzer/repo-name angeben."
        if not c["title"].strip():
            return "Bitte einen App-Namen eingeben."
        if not re.fullmatch(r"[a-z][a-z0-9_]*", c["package"].strip()):
            return "Paketname: nur kleine Buchstaben, Zahlen und _, mit Buchstabe beginnend."
        if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", c["domain"].strip()):
            return "Domain z.B. org.example (klein, mit Punkt)."
        if not re.fullmatch(r"[0-9]+(\.[0-9]+)*", c["version"].strip()):
            return "Version z.B. 1.0"
        return None

    def bauen(self):
        c = self.werte()
        fehler = self.pruefen(c)
        if fehler:
            self.status(fehler)
            return
        self.app.save_settings(c)
        self.bau_btn.disabled = True
        self.dl_btn.disabled = True
        self.log_btn.disabled = True
        threading.Thread(target=self.bau_ablauf, args=(c,), daemon=True).start()

    def bau_ablauf(self, c):
        try:
            gh = GitHub(c["token"], c["repo"])
            self.status("Verbinde mit GitHub …")
            gh.request("GET", f"/repos/{gh.repo}")

            self.status("Lade Workflow hoch …")
            gh.put_file(f".github/workflows/{WORKFLOW_NAME}", WORKFLOW, "APK-Workflow aktualisieren")
            self.status("Lade buildozer.spec hoch …")
            gh.put_file("buildozer.spec", make_spec(c), "buildozer.spec aktualisieren")
            if c["code"].strip():
                self.status("Lade main.py hoch …")
                gh.put_file("main.py", c["code"], "main.py aktualisieren")

            alte = {r["id"] for r in gh.runs()}
            self.status("Starte Build …")
            for versuch in range(8):
                try:
                    gh.request("POST",
                               f"/repos/{gh.repo}/actions/workflows/{WORKFLOW_NAME}/dispatches",
                               {"ref": gh.request("GET", f"/repos/{gh.repo}")["default_branch"]})
                    break
                except RuntimeError:
                    if versuch == 7:
                        raise
                    time.sleep(5)

            run = None
            for _ in range(30):
                time.sleep(4)
                run = next((r for r in gh.runs() if r["id"] not in alte), None)
                if run:
                    break
            if not run:
                raise RuntimeError("Build-Lauf nicht gefunden. Schau unter Actions im Repo nach.")
            self.set_run(run["html_url"])

            start = time.time()
            while run["status"] != "completed":
                minuten = int((time.time() - start) // 60)
                self.status(f"Baue APK … ({run['status']}, {minuten} Min.)\n"
                            "Der erste Build dauert ca. 20–30 Minuten, danach schneller.\n"
                            "Du kannst die App offen lassen.")
                time.sleep(20)
                run = gh.request("GET", f"/repos/{gh.repo}/actions/runs/{run['id']}")

            if run["conclusion"] != "success":
                raise RuntimeError(f"Build fehlgeschlagen ({run['conclusion']}). "
                                   "Tippe auf 'Build ansehen' für das Log.")

            self.status("Hole Download-Link …")
            rel = None
            for _ in range(6):
                try:
                    rel = gh.request("GET", f"/repos/{gh.repo}/releases/tags/build-{run['run_number']}")
                    break
                except RuntimeError:
                    time.sleep(5)
            apks = [a for a in (rel or {}).get("assets", []) if a["name"].endswith(".apk")]
            if not apks:
                raise RuntimeError("Build ok, aber keine APK im Release gefunden.")
            self.fertig(apks[0]["browser_download_url"], apks[0]["name"])
        except Exception as e:
            self.status(f"Fehler: {e}")
            self.bau_ende()

    @mainthread
    def status(self, text):
        self.status_lbl.text = text

    @mainthread
    def set_run(self, url):
        self.run_url = url
        self.log_btn.disabled = False

    @mainthread
    def fertig(self, url, name):
        self.apk_url = url
        self.dl_btn.disabled = False
        self.bau_btn.disabled = False
        self.status_lbl.text = f"Fertig! {name}\nTippe auf 'APK herunterladen'."

    @mainthread
    def bau_ende(self):
        self.bau_btn.disabled = False


class APKBuilderApp(App):
    title = "APK Builder"

    def settings_path(self):
        return os.path.join(self.user_data_dir, "einstellungen.json")

    def load_settings(self):
        try:
            with open(self.settings_path(), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_settings(self, data):
        try:
            with open(self.settings_path(), "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass

    def build(self):
        Window.softinput_mode = "below_target"
        return Oberflaeche(self)


if __name__ == "__main__":
    APKBuilderApp().run()
