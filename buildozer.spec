[app]
title = APK Builder
package.name = apkbuilder
package.domain = org.example
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
version = 1.0
requirements = python3,kivy,certifi
orientation = portrait
fullscreen = 0
android.permissions = INTERNET
android.api = 34
android.minapi = 21
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 0
