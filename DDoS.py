import socket
import struct
import threading
import random
import time
import select
import sys
from mcstatus import JavaServer

# Глоб флаг
go = True

def make_varint(val):
    d = bytearray()
    while 1:
        byte = val & 0x7F
        val >>= 7
        if val:
            d.append(byte | 0x80)
        else:
            d.append(byte)
            break
    return bytes(d)

def get_varint(sock, timeout=5):
    value = 0
    size = 0
    start = time.time()
    
    while 1:
        if time.time() - start > timeout:
            raise TimeoutError("varint timeout")
        
        if select.select([sock], [], [], 0.1)[0]:
            byte = sock.recv(1)
            if not byte:
                raise ConnectionError("conn dead")
                
            b = byte[0]
            value |= (b & 0x7F) << (size * 7)
            size += 1
            
            if size > 5:
                raise ValueError("varint too big")
                
            if not (b & 0x80):
                return value

def send_pack(sock, pid, data):
    pack = bytearray()
    pack.extend(make_varint(pid))
    pack.extend(data)
    len_data = make_varint(len(pack))
    sock.sendall(len_data + pack)

def move_bot(sock, x, y, z, yaw=0.0, pitch=0.0, grounded=1, protocol=340):
    pos_data = bytearray()
    pos_data.extend(struct.pack('>d', x))
    pos_data.extend(struct.pack('>d', y))
    pos_data.extend(struct.pack('>d', z))
    pos_data.extend(struct.pack('>f', yaw))
    pos_data.extend(struct.pack('>f', pitch))
    pos_data.append(grounded)
    # В 1.16.5 пакет движения стал 0x2F, в 1.12.2 — 0x0C
    pid = 0x2F if protocol >= 754 else 0x0C
    send_pack(sock, pid, pos_data)  # move pack

def connect_bot(host, port, bot_name, bid):
    tries = 0
    max_tries = 3
    
    while tries < max_tries and go:
        try:
            s = socket.socket()
            s.settimeout(10.0)
            s.connect((host, port))
            return s
            
        except (socket.timeout, ConnectionRefusedError) as e:
            tries += 1
            print(f"[Bot {bid}] Try {tries}/{max_tries}: {str(e)}")
            time.sleep(random.uniform(1, 3))
        except Exception as e:
            print(f"[Bot {bid}] Conn error: {str(e)}")
            return None
            
    return None

def bot_work(host, port, bot_name, msg, bid, move_gap=5, protocol=340):
    global go
    # Рандомизируем начальную позицию, включая Y
    pos = [
        -232.5 + random.uniform(-5, 5),
        70.0 + random.uniform(0, 20),  # Y теперь тоже рандомный
        239.5 + random.uniform(-5, 5)
    ]
    
    while go:
        try:
            # Подключение
            s = connect_bot(host, port, bot_name, bid)
            if s is None:
                print(f"[Bot {bid}] Cant connect")
                return
                
            print(f"[Bot {bid}] Connected (protocol {protocol})")
            
            # Handshake
            hs = bytearray()
            hs.extend(make_varint(protocol))  # ver
            hs.extend(make_varint(len(host)))
            hs.extend(host.encode())
            hs.extend(struct.pack('>H', port))
            hs.extend(make_varint(2))  # state: login
            send_pack(s, 0, hs)
            
            # Login
            login_data = bytearray()
            login_data.extend(make_varint(len(bot_name)))
            login_data.extend(bot_name.encode())
            send_pack(s, 0, login_data)
            
            # Ждём успешный логин
            logged = 0
            start_t = time.time()
            
            while time.time() - start_t < 10 and go and not logged:
                try:
                    ln = get_varint(s)
                    pid = get_varint(s)
                    
                    if pid == 0x02:  # login ok (1.12.2)
                        uuid_ln = get_varint(s)
                        s.recv(uuid_ln)  # skip uuid
                        name_ln = get_varint(s)
                        s.recv(name_ln)  # skip name
                        print(f"[Bot {bid}] Logged in")
                        logged = 1
                        break
                    elif pid == 0x02 and protocol >= 754:  # login success 1.16.5
                        # UUID (16 байт) + Username
                        s.recv(16)  # skip UUID
                        name_ln = get_varint(s)
                        s.recv(name_ln)  # skip name
                        print(f"[Bot {bid}] Logged in")
                        logged = 1
                        break
                    elif pid == 0x01:  # encryption request (игнорируем для offline)
                        # Пропускаем поля: сервер id, pubkey, verify token
                        server_id_len = get_varint(s)
                        s.recv(server_id_len)
                        pubkey_len = get_varint(s)
                        s.recv(pubkey_len)
                        verify_token_len = get_varint(s)
                        s.recv(verify_token_len)
                        # Не отвечаем — сервер в offline режиме должен пропустить
                        continue
                    elif pid == 0x1F:  # keepalive 1.12.2
                        keep_id = get_varint(s)
                        resp = bytearray()
                        resp.extend(struct.pack('>Q', keep_id))
                        send_pack(s, 0x0B, resp)
                    elif pid == 0x20 and protocol >= 754:  # keepalive 1.16.5
                        keep_id = get_varint(s)
                        resp = bytearray()
                        resp.extend(struct.pack('>Q', keep_id))
                        send_pack(s, 0x0F, resp)  # 0x0F в 1.16.5
                    elif pid == 0x1A:  # disconnect 1.12.2
                        reason_len = get_varint(s)
                        reason = s.recv(reason_len)
                        print(f"[Bot {bid}] Disconnected by server: {reason.decode('utf-8', errors='ignore')}")
                        s.close()
                        return
                    elif pid == 0x19 and protocol >= 754:  # disconnect 1.16.5
                        reason_len = get_varint(s)
                        reason = s.recv(reason_len)
                        print(f"[Bot {bid}] Disconnected by server: {reason.decode('utf-8', errors='ignore')}")
                        s.close()
                        return
                    else:
                        # Пропускаем лишнее
                        left = ln - 1
                        while left > 0:
                            chunk = s.recv(min(1024, left))
                            if not chunk:
                                raise ConnectionError("conn dead")
                            left -= len(chunk)
                except (socket.timeout, TimeoutError):
                    continue
            
            if not logged:
                print(f"[Bot {bid}] Login fail")
                s.close()
                time.sleep(5)
                continue
                
            # После логина отправляем Client Settings (иначе сервер может не считать нас "загруженными")
            settings_data = bytearray()
            settings_data.extend(b'\x07')  # locale (например, "en_US")
            settings_data.extend(b'\x01')  # view distance (10 chunks)
            settings_data.extend(b'\x00')  # chat mode (enabled)
            settings_data.extend(b'\x00')  # chat colors
            settings_data.extend(b'\x7f')  # skin parts (all)
            settings_data.extend(b'\x01')  # main hand (right)
            # В 1.16.5 пакет стал 0x07, в 1.12.2 — 0x15
            settings_pid = 0x07 if protocol >= 754 else 0x15
            send_pack(s, settings_pid, settings_data)
            print(f"[Bot {bid}] Sent client settings")
            
            # Первое сообщение
            chat_data = bytearray()
            chat_data.extend(make_varint(len(msg)))
            chat_data.extend(msg.encode())
            # В 1.16.5 чат стал 0x03, в 1.12.2 — 0x01
            chat_pid = 0x03 if protocol >= 754 else 0x01
            send_pack(s, chat_pid, chat_data)
            print(f"[Bot {bid}] First msg sent")
            
            # Основной цикл
            last_msg = time.time()
            last_moved = time.time()
            last_keep = time.time()
            
            while go:
                now = time.time()
                
                # Keepalive check
                if now - last_keep > 20:
                    resp = bytearray()
                    resp.extend(struct.pack('>Q', 0))
                    # В 1.16.5 — 0x0F, в 1.12.2 — 0x0B
                    keep_pid = 0x0F if protocol >= 754 else 0x0B
                    send_pack(s, keep_pid, resp)
                    last_keep = now
                
                # Отправка сообщений
                if now - last_msg > random.uniform(10, 30):
                    chat_data = bytearray()
                    chat_data.extend(make_varint(len(msg)))
                    chat_data.extend(msg.encode())
                    chat_pid = 0x03 if protocol >= 754 else 0x01
                    send_pack(s, chat_pid, chat_data)
                    last_msg = now
                    print(f"[Bot {bid}] Msg sent")
                
                # Движение
                if now - last_moved > move_gap:
                    pos[0] += random.uniform(-2, 2)
                    pos[2] += random.uniform(-2, 2)
                    # Y тоже немного "плавает" для реализма
                    pos[1] += random.uniform(-0.5, 0.5)
                    # Ограничиваем Y, чтобы не улететь в космос или ад
                    pos[1] = max(50.0, min(120.0, pos[1]))
                    move_bot(s, pos[0], pos[1], pos[2], protocol=protocol)
                    last_moved = now
                
                # Чтение входящих
                try:
                    if select.select([s], [], [], 1.0)[0]:
                        ln = get_varint(s, timeout=2)
                        pid = get_varint(s)
                        
                        if pid == 0x1F:  # keepalive 1.12.2
                            keep_id = get_varint(s)
                            resp = bytearray()
                            resp.extend(struct.pack('>Q', keep_id))
                            send_pack(s, 0x0B, resp)
                            last_keep = now
                        elif pid == 0x20 and protocol >= 754:  # keepalive 1.16.5
                            keep_id = get_varint(s)
                            resp = bytearray()
                            resp.extend(struct.pack('>Q', keep_id))
                            send_pack(s, 0x0F, resp)
                            last_keep = now
                        elif pid == 0x0E:  # chat 1.12.2
                            json_ln = get_varint(s)
                            s.recv(json_ln)
                            s.recv(1)  # position
                        elif pid == 0x0F and protocol >= 754:  # chat 1.16.5
                            # Может быть несколько форматов — пропускаем полностью
                            left = ln - 1
                            while left > 0:
                                chunk = s.recv(min(1024, left))
                                if not chunk:
                                    raise ConnectionError("conn dead")
                                left -= len(chunk)
                        elif pid == 0x1A:  # disconnect 1.12.2
                            reason_len = get_varint(s)
                            reason = s.recv(reason_len)
                            print(f"[Bot {bid}] Disconnected by server: {reason.decode('utf-8', errors='ignore')}")
                            s.close()
                            return
                        elif pid == 0x19 and protocol >= 754:  # disconnect 1.16.5
                            reason_len = get_varint(s)
                            reason = s.recv(reason_len)
                            print(f"[Bot {bid}] Disconnected by server: {reason.decode('utf-8', errors='ignore')}")
                            s.close()
                            return
                        else:
                            # Пропуск
                            left = ln - 1
                            while left > 0:
                                chunk = s.recv(min(1024, left))
                                if not chunk:
                                    raise ConnectionError("conn dead")
                                left -= len(chunk)
                except (socket.timeout, TimeoutError):
                    continue
                except Exception as e:
                    print(f"[Bot {bid}] Pack error: {str(e)}")
                    break
            
            # Отключение
            s.close()
            print(f"[Bot {bid}] Closed")
            return
            
        except (ConnectionResetError, BrokenPipeError):
            print(f"[Bot {bid}] Conn broke, reconnect...")
            time.sleep(random.uniform(3, 7))
            continue
        except Exception as e:
            print(f"[Bot {bid}] Fatal: {str(e)}")
            time.sleep(10)
            continue

def stop_signal(sig, frame):
    global go
    print("\nStopping bots...")
    go = False
    time.sleep(3)
    sys.exit(0)

def main():
    global go
    
    print("""
    ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
    █░▄▄▀█░▄▄▀█░▄▄▀█░▄▄▀█░▄▄▀█░▄▄▀█░▄▄▀█░▄▄▀█░▄▄▀█░▄▄
    █░▀▀░█░▀▀▄█░▀▀░█░▀▀░█░▀▀▄█░▀▀░█░▀▀░█░▀▀▄█░▀▀░█░▄▄
    █░██░█▄██▄█▄██▄█▄██▄█▄█▄▄█▄██▄█▄██▄█▄█▄▄█▄██▄█▄▄▄
    ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
    Minecraft Bot Tool v3
    """)
    
    # Хук на Ctrl+C
    import signal
    signal.signal(signal.SIGINT, stop_signal)
    
    # Юзер инпут
    host_in = input("Server IP: ").strip()
    port_in = int(input("Port [25565]: ") or 25565)
    bot_count = int(input("Bots count: "))
    bot_base = input("Bot name base: ").strip()
    msg_in = input("Spam msg: ").strip()
    move_gap_in = int(input("Move gap [5]: ") or 5)
    
    # Определяем протокол по статусу сервера
    protocol = 777  # по умолчанию 1.12.2
    try:
        srv = JavaServer(host_in, port_in)
        status = srv.status()
        ver_str = status.version.name.lower()
        print(f"\nServer version: {status.version.name}")
        print(f"Players: {status.players.online}/{status.players.max}")
        
        if "1.16" in ver_str or "1.17" in ver_str or "1.18" in ver_str or "1.19" in ver_str:
            protocol = 754  # 1.16.5
            print("→ Using protocol 754 (1.16.5)")
        else:
            print("→ Using protocol 340 (1.12.2)")
            
    except Exception as e:
        print(f"\nStatus check fail: {str(e)}. Using default protocol 340.")
    
    # Старт ботов
    print("\nStarting bots... (Ctrl+C to stop)")
    threads = []
    
    for i in range(bot_count):
        bot_name = f"{bot_base}_{random.randint(1000, 9999)}"
        t = threading.Thread(
            target=bot_work, 
            args=(host_in, port_in, bot_name, msg_in, i+1, move_gap_in, protocol),
            daemon=1
        )
        t.start()
        threads.append(t)
        time.sleep(random.uniform(0.5, 2.0))
    
    # Мейн луп
    try:
        while go:
            time.sleep(1)
            alive = sum(1 for t in threads if t.is_alive())
            print(f"\rBots alive: {alive}/{bot_count}", end="")
            sys.stdout.flush()
    except KeyboardInterrupt:
        stop_signal(signal.SIGINT, None)

if __name__ == "__main__":
    main()
    # 𝕜𝕖𝕣𝕚𝕜𝕦𝕤𝕙
