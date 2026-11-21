# strangling-hand-deploy

Скрипты развёртывания узлов Savebit VPN: входов (российские серверы для клиентов) и выходов (зарубежные серверы).
Узел ставится одной командой, дальше им управляет панель `api.savebit.ru` через агента.

```
Happ ──► Вход (Россия) ──► Выход (за рубежом) ──► Интернет
            ▲                    ▲
            └── агент ──► api.savebit.ru ◄── агент
```

## Как это работает

1. В админке `net.savebit.ru` нажимаешь «Добавить узел», выбираешь роль (вход или выход) и получаешь **ключ узла**.
2. На свежем сервере запускаешь установщик. Он спрашивает **ключ** и **домен API** (по умолчанию `api.savebit.ru`).
3. Установщик проверяет ключ, ставит агента и нужную версию Xray, применяет конфиг узла и запускает службы.
4. Дальше **агент** раз в минуту сам забирает у API актуальный конфиг (новые друзья, новые выходы, новая версия Xray),
   проверяет его, применяет и отправляет отчёт о здоровье узла. Руками на сервере ничего делать не нужно.

Соединения всегда идут **от узла к API**. Панели не нужен SSH-доступ к серверам, а ключ узла открывает только его собственный конфиг.
Если API недоступен или ключ не принят, узел продолжает работать на последнем рабочем конфиге.

## Установка

Вход:

```bash
curl -fsSL https://raw.githubusercontent.com/knyazevi81/strangling-hand-deploy/main/deploy-entry.sh | sudo bash
```

Выход:

```bash
curl -fsSL https://raw.githubusercontent.com/knyazevi81/strangling-hand-deploy/main/deploy-exit.sh | sudo bash
```

Скрипт спросит:

```
Ключ узла: ********
Домен API [api.savebit.ru]:
```

Роль узла берётся из API по ключу. `deploy-entry.sh` и `deploy-exit.sh` дополнительно проверяют, что ключ выдан именно для этой роли,
чтобы случайно не превратить зарубежный сервер во вход. Есть и общий `install.sh` без такой проверки.

Без вопросов, например из своего скрипта:

```bash
curl -fsSL https://raw.githubusercontent.com/knyazevi81/strangling-hand-deploy/main/install.sh \
  | sudo bash -s -- --key sbn_... --api api.savebit.ru --yes
```

Требования: Ubuntu 20.04+ или Debian 11+, root, systemd, Python 3.8+ (есть в системе по умолчанию). Процессор x86_64 или arm64.

## Ключ узла

Ключ можно быстро поменять: перевыпускаешь его в админке, старый сразу перестаёт работать, узел при этом **продолжает работать**
на последнем конфиге и пишет в журнал, что ключ не принят. На сервере:

```bash
savebit-agent rekey sbn_НОВЫЙ_КЛЮЧ
```

Агент сначала проверит новый ключ и только потом заменит старый. Можно и просто запустить установщик ещё раз с новым ключом.

Сменить адрес API: `savebit-agent set-api api2.savebit.ru`.

## Команды на сервере

| Команда | Что делает |
|---|---|
| `savebit-agent status` | узел, роль, ревизия конфига, версия Xray, последняя ошибка, отпечаток сертификата |
| `savebit-agent sync` | синхронизироваться прямо сейчас, не дожидаясь минуты |
| `savebit-agent check` | проверить ключ и API, ничего не меняя |
| `savebit-agent rekey КЛЮЧ` | заменить ключ |
| `journalctl -u savebit-agent -f` | журнал агента |
| `journalctl -u savebit-xray -f` | журнал Xray |

## Что ставится

| Путь | Что это |
|---|---|
| `/opt/savebit/agent/savebit_agent.py` | агент (только стандартная библиотека Python), ссылка `/usr/local/bin/savebit-agent` |
| `/opt/savebit/xray/versions/<версия>/` | Xray и geo-файлы; `current` указывает на нужную версию, хранятся 3 последние |
| `/etc/savebit/agent.env` | адрес API и ключ узла, права 600 |
| `/etc/savebit/xray/config.json` | текущий конфиг Xray (присылает API) |
| `/etc/savebit/tls/` | самоподписанные сертификаты для Hysteria2, создаются по запросу API |
| `/var/lib/savebit/state.json` | ревизия, время синхронизации, последняя ошибка |
| `savebit-agent.service` | агент, root |
| `savebit-xray.service` | Xray от пользователя `savebit`, с правом слушать порты ниже 1024 |
| `/etc/sysctl.d/99-savebit.conf` | BBR и большие буферы для QUIC |

Если включён ufw, агент сам открывает порты из конфига (TCP для VLESS, UDP для Hysteria2). Закрывать порты сам не будет.

## Как агент применяет конфиг

1. `GET /api/v1/agent/config` с ревизией текущего конфига. Если ничего не поменялось, API отвечает `304`, и всё.
2. Создаёт сертификаты, которые просит API, и ставит нужную версию Xray (с проверкой контрольной суммы).
3. Проверяет новый конфиг командой `xray -test`. Не прошёл: остаётся на старом и сообщает об ошибке.
4. Подменяет конфиг и перезапускает Xray. Не поднялся: возвращает прежний конфиг и сообщает об ошибке.
5. Открывает порты в ufw и отправляет отчёт `POST /api/v1/agent/report`.

Контракт для бэкенда: [docs/agent-api.md](docs/agent-api.md). Примеры конфигов: [examples/entry.json](examples/entry.json),
[examples/exit.json](examples/exit.json). Оба проверены на Xray 26.11.9: цепочка клиент → вход → выход работает по всем
трём профилям, а при отказе канала XHTTP вход сам переключается на Hysteria2.

## Удаление

```bash
curl -fsSL https://raw.githubusercontent.com/knyazevi81/strangling-hand-deploy/main/install.sh | sudo bash -s -- --uninstall
# вместе с ключом, конфигами и сертификатами:
curl -fsSL https://raw.githubusercontent.com/knyazevi81/strangling-hand-deploy/main/install.sh | sudo bash -s -- --uninstall --purge
```

## Если что-то не работает

- **«API не принял ключ»**: ключ перевыпустили или опечатка. Возьми новый в админке и выполни `savebit-agent rekey`.
- **«API недоступен»**: сервер не достаёт до `api.savebit.ru`. Проверь `curl -I https://api.savebit.ru` с сервера.
- **«конфиг не прошёл проверку xray -test»**: бэкенд собрал неверный конфиг. Узел работает на старом, текст ошибки есть в отчёте и в `savebit-agent status`.
- **«Xray не запустился с новым конфигом, вернул прежний»**: чаще всего порт занят другой программой (например, x-ui). `ss -tlnup | grep :443`.

## Разработка и тесты

Тестовый API реализует тот же контракт, что и бэкенд:

```bash
python3 tools/mock_api.py --nodes tools/mock_nodes.example.json --port 18765
```

Смоук-тест агента без systemd и без системных путей (32 проверки: установка Xray, применение, 304, битый конфиг, откат,
смена ключа, отчёт):

```bash
XRAY_ZIP=/путь/к/Xray-linux-64.zip bash tests/smoke.sh
```

```
agent/savebit_agent.py     агент
install.sh                 установщик
deploy-entry.sh            установщик входа (проверяет роль)
deploy-exit.sh             установщик выхода (проверяет роль)
docs/agent-api.md          контракт API для бэкенда
examples/                  эталонные конфиги входа и выхода
tools/mock_api.py          тестовый API
tests/                     смоук-тест и заменитель systemctl
```
