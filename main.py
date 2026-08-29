"""
ESP32 Wireless IDE - arquivo unico para MicroPython

Salve este arquivo como main.py no ESP32 usando o Thonny.
Depois, alimente apenas o ESP32, conecte-se ao Wi-Fi criado e abra:
http://192.168.4.1

Arquivos do usuario ficam em /workspace. O main.py e protegido para que
o servidor web continue disponivel para futuras edicoes sem cabo.
"""

import network
import socket
import os
import time
import urandom

try:
    import machine
except ImportError:
    machine = None

try:
    import ujson as json
except ImportError:
    import json


# ============================================================
# CONFIGURACAO - altere as senhas antes de enviar para o ESP32
# ============================================================

WIFI_NAME = "ESP32-IDE"
WIFI_PASSWORD = "*******"  # minimo de 8 caracteres
WEB_PASSWORD = "*********"

WORKSPACE = "/workspace"
PORT = 80
MAX_BODY = 48 * 1024  # tamanho maximo de um arquivo enviado pelo navegador

SESSION = None


# ============================================================
# WIFI ACCESS POINT
# ============================================================

def start_wifi():
    # MicroPython usa nomes diferentes em algumas versoes.
    try:
        interface = network.WLAN.IF_AP
    except AttributeError:
        interface = network.AP_IF

    ap = network.WLAN(interface)
    ap.active(True)

    try:
        ap.config(ssid=WIFI_NAME, password=WIFI_PASSWORD)
    except Exception:
        # Firmwares mais antigos usam "essid".
        ap.config(essid=WIFI_NAME, password=WIFI_PASSWORD)

    time.sleep(1)
    return ap


def make_workspace():
    try:
        os.mkdir(WORKSPACE)
    except OSError:
        pass


# ============================================================
# UTILITARIOS
# ============================================================

def new_session():
    global SESSION

    try:
        SESSION = "%08x%08x" % (
            urandom.getrandbits(32),
            urandom.getrandbits(32)
        )
    except Exception:
        SESSION = str(time.ticks_ms())

    return SESSION


def url_decode(value):
    """Decodifica parametros da URL sem precisar de urllib."""
    value = value.replace("+", " ")
    output = bytearray()
    index = 0

    while index < len(value):
        if value[index] == "%" and index + 2 < len(value):
            try:
                output.append(int(value[index + 1:index + 3], 16))
                index += 3
                continue
            except ValueError:
                pass

        output.extend(value[index].encode())
        index += 1

    try:
        return output.decode()
    except UnicodeError:
        raise ValueError("Texto da URL invalido")


def get_param(target, name):
    if "?" not in target:
        return ""

    query = target.split("?", 1)[1]

    for part in query.split("&"):
        if "=" in part:
            key, value = part.split("=", 1)
            if key == name:
                return value

    return ""


def safe_path(value, allow_root=False):
    """Retorna um caminho dentro de /workspace e bloqueia '..'."""
    value = url_decode(value).replace("\\", "/")

    while value.startswith("/"):
        value = value[1:]

    parts = []
    for part in value.split("/"):
        if not part or part == ".":
            continue
        if part == ".." or "\x00" in part:
            raise ValueError("Caminho invalido")
        parts.append(part)

    if not parts:
        if allow_root:
            return WORKSPACE
        raise ValueError("Informe um nome")

    return WORKSPACE + "/" + "/".join(parts)


def relative_path(full_path):
    if full_path == WORKSPACE:
        return "/"
    return full_path[len(WORKSPACE):]


def is_directory(path):
    try:
        return bool(os.stat(path)[0] & 0x4000)
    except OSError:
        return False


def path_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def parent_path(path):
    position = path.rfind("/")
    if position <= 0:
        return "/"
    return path[:position]


# ============================================================
# HTTP
# ============================================================

def send_all(client, data):
    while data:
        sent = client.send(data)
        if not sent:
            break
        data = data[sent:]


def send_response(client, body=b"", content_type="text/plain", status="200 OK"):
    if isinstance(body, str):
        body = body.encode()

    header = (
        "HTTP/1.1 " + status + "\r\n"
        "Content-Type: " + content_type + "\r\n"
        "Content-Length: " + str(len(body)) + "\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n"
        "\r\n"
    )

    send_all(client, header.encode())
    if body:
        send_all(client, body)


def send_json(client, object_to_send, status="200 OK"):
    send_response(client, json.dumps(object_to_send), "application/json", status)


def send_error(client, message, status="400 Bad Request"):
    send_json(client, {"ok": False, "error": message}, status)


def receive_request(client):
    """Le cabecalho e depois todo o body de acordo com Content-Length."""
    data = b""

    while b"\r\n\r\n" not in data:
        chunk = client.recv(1024)
        if not chunk:
            break
        data += chunk
        if len(data) > 8192:
            raise ValueError("Cabecalho HTTP muito grande")

    if b"\r\n\r\n" not in data:
        raise ValueError("Requisicao HTTP incompleta")

    header, body = data.split(b"\r\n\r\n", 1)
    content_length = 0

    for line in header.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            try:
                content_length = int(line.split(b":", 1)[1].strip())
            except ValueError:
                raise ValueError("Content-Length invalido")

    if content_length < 0 or content_length > MAX_BODY:
        raise ValueError("Arquivo muito grande (maximo: 48 KB)")

    while len(body) < content_length:
        chunk = client.recv(min(2048, content_length - len(body)))
        if not chunk:
            break
        body += chunk

    if len(body) != content_length:
        raise ValueError("Envio do arquivo incompleto")

    return header, body


def request_session(header):
    for line in header.split(b"\r\n"):
        if line.lower().startswith(b"x-session:"):
            try:
                return line.split(b":", 1)[1].strip().decode()
            except UnicodeError:
                return ""
    return ""


def is_logged_in(header):
    return SESSION is not None and request_session(header) == SESSION


# ============================================================
# ARQUIVOS
# ============================================================

def list_directory(path):
    items = []

    for name in os.listdir(path):
        full_path = path + "/" + name
        try:
            stat = os.stat(full_path)
            directory = bool(stat[0] & 0x4000)
            size = 0 if directory else stat[6]
            items.append({
                "name": name,
                "path": relative_path(full_path),
                "dir": directory,
                "size": size
            })
        except OSError:
            pass

    # Pastas antes dos arquivos. Sem lambda, para maior compatibilidade.
    items.sort(key=lambda item: (not item["dir"], item["name"].lower()))
    return items


def write_file(path, data):
    folder = parent_path(path)
    if not is_directory(folder):
        raise ValueError("A pasta de destino nao existe")

    temporary = path + ".new"
    with open(temporary, "wb") as file:
        file.write(data)

    try:
        os.remove(path)
    except OSError:
        pass

    os.rename(temporary, path)


def remove_file(path):
    if is_directory(path):
        raise ValueError("Abra a pasta e exclua os arquivos dela primeiro")
    os.remove(path)


class ConsoleCapture:
    """Guarda a saida de print() para mostrar no navegador."""

    def __init__(self):
        self.text = ""

    def write(self, data):
        self.text += str(data)
        # Evita que muitos prints gastem toda a memoria do ESP32.
        if len(self.text) > 4096:
            self.text = self.text[-4096:]

    def flush(self):
        pass


def run_file(path):
    """Executa um .py curto e retorna o que ele imprimiu.

    Nao use while True neste modo: um programa infinito impede o servidor
    de responder ate que o ESP32 seja reiniciado.
    """
    if not path.endswith(".py"):
        raise ValueError("Apenas arquivos .py podem ser executados")

    console = ConsoleCapture()

    def console_print(*values, **options):
        separator = options.get("sep", " ")
        ending = options.get("end", "\n")
        text = ""

        for index in range(len(values)):
            if index:
                text += separator
            text += str(values[index])

        console.write(text + ending)

    success = True

    try:
        with open(path, "r") as file:
            source = file.read()

        # O print definido aqui aparece na tela da IDE.
        scope = {
            "__name__": "__main__",
            "__file__": path,
            "print": console_print
        }
        exec(source, scope, scope)
    except Exception as error:
        success = False
        console.write("\nERRO: " + str(error) + "\n")

    return success, console.text


# ============================================================
# PAGINA WEB
# ============================================================

HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ESP32 Wireless IDE</title>
<style>
*{box-sizing:border-box} body{margin:0;background:#07101f;color:#e5e7eb;font-family:Arial,sans-serif}
header{height:58px;padding:18px;background:#101827;border-bottom:1px solid #273449;font-size:19px;font-weight:bold}
#login{max-width:340px;margin:100px auto;padding:24px;background:#101827;border-radius:10px}
input,textarea{background:#050b16;color:#fff;border:1px solid #334155} input{width:100%;padding:11px;border-radius:6px}
button{background:#2563eb;color:#fff;border:0;border-radius:6px;padding:9px 11px;cursor:pointer}.green{background:#059669}.gray{background:#475569}.danger{background:#dc2626}
#ide{display:none;height:calc(100vh - 58px)}.layout{display:grid;grid-template-columns:280px 1fr;height:100%}.side{overflow:auto;padding:12px;background:#0b1220;border-right:1px solid #273449}.editor{display:flex;flex-direction:column;min-width:0}.tools{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}.bar{padding:9px;background:#101827;border-bottom:1px solid #273449;display:flex;flex-wrap:wrap;align-items:center;gap:6px}.path,#filename{font-family:monospace;color:#93c5fd;overflow-wrap:anywhere}.path{margin:9px 0}.item{font-family:monospace;padding:9px;border-radius:5px;cursor:pointer}.item:hover{background:#1e293b}textarea{flex:1;width:100%;padding:14px;border:0;outline:0;resize:none;font:14px/1.5 monospace}#status{min-height:34px;padding:9px 12px;background:#020617;border-top:1px solid #273449;color:#a7f3d0;font-family:monospace;white-space:pre-wrap}
@media(max-width:700px){.layout{grid-template-columns:1fr;grid-template-rows:230px 1fr}.side{border-right:0;border-bottom:1px solid #273449}}
</style>
</head>
<body>
<header>⚡ ESP32 Wireless IDE</header>

<section id="login"><h2>Entrar</h2><input id="password" type="password" placeholder="Senha"><p><button onclick="login()">Entrar</button></p><span id="loginError"></span></section>

<section id="ide"><div class="layout">
<aside class="side"><div class="tools"><button onclick="goUp()">⬆</button><button onclick="refresh()">↻</button><button class="green" onclick="newFile()">+ Arquivo</button><button class="green" onclick="newFolder()">+ Pasta</button></div><div id="path" class="path">/</div><div id="files"></div></aside>
<main class="editor"><div class="bar"><span id="filename">Nenhum arquivo aberto</span><button onclick="save()">💾 Salvar</button><button class="green" onclick="runCurrent()">▶ Executar</button><button class="gray" onclick="renameCurrent()">Renomear</button><button class="danger" onclick="deleteCurrent()">Excluir</button><button class="danger" onclick="reboot()">Reiniciar ESP32</button></div><textarea id="editor" spellcheck="false" placeholder="Selecione ou crie um arquivo..."></textarea><div id="status">Pronto</div></main>
</div></section>

<script>
let session=localStorage.getItem("esp32_session"),currentDir="/",currentFile="";
function status(text){document.getElementById("status").textContent=text}
function showIDE(){document.getElementById("login").style.display="none";document.getElementById("ide").style.display="block"}
async function login(){let r=await fetch("/api/login",{method:"POST",body:document.getElementById("password").value});if(!r.ok){document.getElementById("loginError").textContent="Senha incorreta.";return}session=await r.text();localStorage.setItem("esp32_session",session);showIDE();refresh()}
async function api(url,options={}){options.headers=options.headers||{};options.headers["X-Session"]=session;let r=await fetch(url,options);if(r.status===401){localStorage.removeItem("esp32_session");location.reload()}return r}
async function refresh(){let r=await api("/api/list?path="+encodeURIComponent(currentDir));if(!r.ok){status("Erro ao listar arquivos");return}let items=await r.json(),box=document.getElementById("files");document.getElementById("path").textContent=currentDir;box.innerHTML="";if(!items.length){box.textContent="Pasta vazia"}for(let item of items){let row=document.createElement("div");row.className="item";row.textContent=(item.dir?"📁 ":"📄 ")+item.name;row.onclick=()=>item.dir?openDir(item.path):openFile(item.path);box.appendChild(row)}}
function openDir(path){currentDir=path;refresh()}function goUp(){if(currentDir==="/")return;let p=currentDir.split("/");p.pop();currentDir=p.join("/")||"/";refresh()}
async function openFile(path){let r=await api("/api/read?path="+encodeURIComponent(path));if(!r.ok){status("Nao foi possivel abrir o arquivo");return}currentFile=path;document.getElementById("filename").textContent=path;document.getElementById("editor").value=await r.text();status("Arquivo aberto")}
async function save(){if(!currentFile){alert("Nenhum arquivo aberto.");return}status("Salvando...");let r=await api("/api/write?path="+encodeURIComponent(currentFile),{method:"POST",body:document.getElementById("editor").value});let result=await r.json();status(result.ok?"✓ Arquivo salvo":"Erro: "+result.error);if(result.ok)refresh()}
async function runCurrent(){if(!currentFile){alert("Nenhum arquivo aberto.");return}if(!currentFile.endsWith(".py")){alert("Apenas arquivos .py podem ser executados.");return}if(!confirm("Execute apenas testes curtos. Nao use while True, pois sera preciso reiniciar o ESP32.\n\nExecutar "+currentFile+"?"))return;status("Executando...");let r=await api("/api/run?path="+encodeURIComponent(currentFile),{method:"POST"}),result;try{result=await r.json()}catch(error){status("ERRO HTTP "+r.status+": resposta invalida");return}if(result.ok){status("✓ Resultado:\n"+(result.output||"(sem saida)"))}else{status("ERRO: "+(result.error||result.output||("HTTP "+r.status)))}}
function pathInCurrent(name){return currentDir==="/"?"/"+name:currentDir+"/"+name}
async function newFile(){let name=prompt("Nome do arquivo:","novo.py");if(!name)return;let path=pathInCurrent(name),r=await api("/api/write?path="+encodeURIComponent(path),{method:"POST",body:""});let result=await r.json();if(!result.ok){alert(result.error);return}await refresh();openFile(path)}
async function newFolder(){let name=prompt("Nome da pasta:","nova_pasta");if(!name)return;let r=await api("/api/mkdir?path="+encodeURIComponent(pathInCurrent(name)),{method:"POST"}),result=await r.json();if(!result.ok)alert(result.error);refresh()}
async function deleteCurrent(){if(!currentFile||!confirm("Excluir "+currentFile+"?"))return;let r=await api("/api/delete?path="+encodeURIComponent(currentFile),{method:"POST"}),result=await r.json();if(!result.ok){alert(result.error);return}currentFile="";document.getElementById("editor").value="";document.getElementById("filename").textContent="Nenhum arquivo aberto";refresh()}
async function renameCurrent(){if(!currentFile)return;let name=prompt("Novo nome (na mesma pasta):",currentFile.split("/").pop());if(!name)return;let slash=currentFile.lastIndexOf("/"),next=currentFile.slice(0,slash+1)+name,r=await api("/api/rename?old="+encodeURIComponent(currentFile)+"&new="+encodeURIComponent(next),{method:"POST"}),result=await r.json();if(!result.ok){alert(result.error);return}currentFile=next;document.getElementById("filename").textContent=next;refresh()}
async function reboot(){if(!confirm("Reiniciar o ESP32 agora?"))return;status("Reiniciando...");await api("/api/reboot",{method:"POST"})}
if(session){showIDE();refresh()}
</script>
</body>
</html>"""


# ============================================================
# ROTAS DA API
# ============================================================

def handle_request(client, header, body):
    first_line = header.split(b"\r\n", 1)[0].decode()
    parts = first_line.split(" ")

    if len(parts) < 2:
        raise ValueError("Requisicao HTTP invalida")

    method = parts[0]
    target = parts[1]
    route = target.split("?", 1)[0]

    if method == "GET" and route == "/":
        send_response(client, HTML, "text/html; charset=utf-8")
        return

    if method == "POST" and route == "/api/login":
        try:
            password = body.decode().strip()
        except UnicodeError:
            password = ""

        if password != WEB_PASSWORD:
            send_error(client, "Senha incorreta", "401 Unauthorized")
            return

        send_response(client, new_session(), "text/plain")
        return

    if not is_logged_in(header):
        send_error(client, "Login necessario", "401 Unauthorized")
        return

    if method == "GET" and route == "/api/list":
        path = safe_path(get_param(target, "path"), True)
        if not is_directory(path):
            raise ValueError("Pasta nao encontrada")
        send_json(client, list_directory(path))
        return

    if method == "GET" and route == "/api/read":
        path = safe_path(get_param(target, "path"))
        if is_directory(path):
            raise ValueError("Este caminho e uma pasta")
        with open(path, "rb") as file:
            send_response(client, file.read(), "text/plain; charset=utf-8")
        return

    if method == "POST" and route == "/api/write":
        path = safe_path(get_param(target, "path"))
        write_file(path, body)
        send_json(client, {"ok": True})
        return

    if method == "POST" and route == "/api/mkdir":
        path = safe_path(get_param(target, "path"))
        if path_exists(path):
            raise ValueError("Ja existe um arquivo ou pasta com esse nome")
        if not is_directory(parent_path(path)):
            raise ValueError("A pasta de destino nao existe")
        os.mkdir(path)
        send_json(client, {"ok": True})
        return

    if method == "POST" and route == "/api/run":
        path = safe_path(get_param(target, "path"))
        success, output = run_file(path)
        send_json(client, {"ok": success, "output": output})
        return

    if method == "POST" and route == "/api/delete":
        path = safe_path(get_param(target, "path"))
        remove_file(path)
        send_json(client, {"ok": True})
        return

    if method == "POST" and route == "/api/rename":
        old_path = safe_path(get_param(target, "old"))
        new_path = safe_path(get_param(target, "new"))
        if not path_exists(old_path):
            raise ValueError("Arquivo original nao encontrado")
        if path_exists(new_path):
            raise ValueError("Ja existe outro arquivo com esse nome")
        if not is_directory(parent_path(new_path)):
            raise ValueError("A pasta de destino nao existe")
        os.rename(old_path, new_path)
        send_json(client, {"ok": True})
        return

    if method == "POST" and route == "/api/reboot":
        send_json(client, {"ok": True, "message": "Reiniciando"})
        time.sleep_ms(250)
        if machine:
            machine.reset()
        return

    send_error(client, "Rota nao encontrada", "404 Not Found")


# ============================================================
# INICIALIZACAO
# ============================================================

def start_server():
    address = socket.getaddrinfo("0.0.0.0", PORT)[0][-1]
    server = socket.socket()

    try:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except AttributeError:
        pass

    server.bind(address)
    server.listen(2)

    print()
    print("================================")
    print("      ESP32 WIRELESS IDE")
    print("================================")
    print("Wi-Fi:", WIFI_NAME)
    print("Abra: http://192.168.4.1")
    print("Arquivos: /workspace")

    while True:
        client = None
        try:
            client, remote = server.accept()
            header, body = receive_request(client)
            handle_request(client, header, body)
        except Exception as error:
            print("HTTP:", error)
            if client:
                try:
                    send_error(client, str(error))
                except Exception:
                    pass
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass


make_workspace()
access_point = start_wifi()

try:
    print("IP:", access_point.ifconfig()[0])
except Exception:
    pass

start_server()

