from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_name: str = "Nestock｜窩有"
    demo_household_id: str = "demo-household"


settings = Settings()
