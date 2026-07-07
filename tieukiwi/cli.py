from dotenv import load_dotenv; load_dotenv()
from .agent import ask
from .ask_ollama import ask_ollama
if __name__ == "__main__":
    print("Tieu Kiwi CLI — type a question (Ctrl+C to exit)")
    while True:
        q = input("\n> ")
        print(ask(q))