import botmine
import threading
import time
import random

bot_names = [f"Bot_{i}" for i in range(1, 30)] # Any amount of bots as long as python can hold them 


def move_loop(bot, name): # Move arond spam
    while True:
        try:
            x = random.uniform(-10, 10)
            z = random.uniform(-10, 10)
            bot.goto(x=x, y=64, z=z)
        except Exception as e:
            break

def spam_loop(bot, name, interval=0.5): # Chat spam
    while True:
        try:
            bot.say("Hello world")
            print(f"[{name}] sent message!")
        except Exception as e:
            bot.wait()
        time.sleep(interval)

def run_bot(name):
    while True:  # Outer loop: if connection fails or disconnects, reconnect
        try:
            with botmine.Bot("127.0.0.1", 25565, nickname=name, version="26.1.2") as bot: # Bot settings
                print(f"[{name}] connected")
                threading.Thread(target=spam_loop, args=(bot, name), daemon=True).start()
                threading.Thread(target=move_loop, args=(bot, name), daemon=True).start()
                bot.reconnect(True)
                bot.wait()
        except Exception as e:
            print(f"[{name}] error: {e}, retrying in 3s...")
            time.sleep(0.5)


threads = []
for name in bot_names:
    t = threading.Thread(target=run_bot, args=(name,), daemon=True)
    t.start()
    threads.append(t)

for t in threads:
    t.join()
