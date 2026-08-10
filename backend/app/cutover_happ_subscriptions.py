import argparse

from app.database import Base, SessionLocal, engine
from app.services.subscriptions import rollback_global_cutover, run_global_cutover


def main() -> None:
    parser = argparse.ArgumentParser(description="Retire or restore all pre-onboarding HAPP subscriptions")
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        if args.rollback:
            print(f"restored={rollback_global_cutover(db)}")
        else:
            result = run_global_cutover(db)
            print(f"cutover={result.cutover_key} retired={result.retired_count}")


if __name__ == "__main__":
    main()
