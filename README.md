# Majestic Tools

Швейный бот, чистильщик ОЗУ и калькулятор готовки с таймерами и уведомлениями в Telegram.

Токен Telegram хранится только локально в cooking_config.json рядом с exe и в репозиторий не попадает.

## Как выпустить обновление
1. Залей изменённые файлы в репозиторий (Add file → Upload files).
2. Releases → Draft a new release → в поле тега впиши новую версию, например `v1.0.1`
   (номер всегда больше прошлого) → в описании можно написать, что нового → Publish release.
3. Через 3–5 минут GitHub сам соберёт `MajesticTools.exe` и приложит его к релизу
   (ход сборки видно во вкладке Actions).
4. Программа при запуске сама найдёт новую версию и предложит обновиться.

## Первая установка
Скачай `MajesticTools.exe` со страницы последнего релиза и положи в отдельную папку.
Настройки сохраняются рядом с exe.

## Сборка вручную
```
py -m pip install -r requirements.txt pyinstaller
py -m PyInstaller --onefile --noconsole --uac-admin --clean --name MajesticTools majestic_tools.py
```
