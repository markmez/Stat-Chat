"""
Baseball Stats Engine — CLI Proof of Concept

Interactive terminal app: type a baseball question, get a data-backed answer.

Usage:
    python3 cli_poc.py
"""

from query_engine import QueryEngine


def main():
    print("=" * 60)
    print("  Baseball Stats Engine (POC)")
    print("  Ask any question about 2024 MLB batting stats.")
    print("  Type 'quit' to exit.")
    print("=" * 60)
    print()

    engine = QueryEngine()

    while True:
        try:
            question = input("⚾ ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not question:
            continue
        if question.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        print()
        answer = engine.ask(question)
        print(answer)
        print()


if __name__ == "__main__":
    main()
