from datetime import date
import math
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .catalog import validated_parameters


class JobRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    hazard: Literal['avalanche', 'landslide', 'wildfire']
    bbox: tuple[float, float, float, float]
    parameters: dict[str, float | int | str] = Field(default_factory=dict)
    buffer: float | None = Field(default=None, ge=0, le=20000)
    resolution: float = Field(default=30, ge=1, le=1000)
    dem_id: UUID | None = None
    rail_id: UUID | None = None
    soil_id: UUID | None = None
    fuel_id: UUID | None = None
    release_id: UUID | None = None
    weather_id: UUID | None = None
    ignitions: list[tuple[float, float]] = Field(default_factory=list, max_length=100)
    scenario_date: date = Field(default_factory=date.today)
    satellite: bool = True
    lookback_days: int = Field(default=60, ge=1, le=365)
    warmup_days: int = Field(default=60, ge=1, le=90)
    corridor: float = Field(default=15, ge=1, le=1000)
    frequency: float | None = Field(default=None, ge=0)
    vulnerability: float = Field(default=.5, ge=0, le=1)
    repair_cost: float = Field(default=0, ge=0)
    release_min_slope: float = Field(default=30, gt=0, lt=90)
    release_max_slope: float = Field(default=50, gt=0, lt=90)
    release_min_relief: float = Field(default=100, ge=0)
    release_min_area: float = Field(default=3600, gt=0)

    @model_validator(mode='after')
    def validate_scenario(self):
        west, south, east, north = self.bbox
        if not (-180 <= west < east <= 180 and -80 <= south < north <= 80 and east-west <= 6):
            raise ValueError('Проверьте границы участка: запад < восток, юг < север; широты от −80 до 80°.')
        if self.release_min_slope >= self.release_max_slope:
            raise ValueError('Нижний уклон очага должен быть меньше верхнего.')
        for lon, lat in self.ignitions:
            if not (math.isfinite(lon) and math.isfinite(lat) and west <= lon <= east and south <= lat <= north):
                raise ValueError('Очаг пожара должен находиться внутри выделенного участка.')
        self.parameters = validated_parameters(self.hazard, self.parameters)
        if self.hazard == 'wildfire' and not self.satellite and not self.fuel_id:
            raise ValueError('Без Sentinel-2 загрузите GeoJSON зон горючего с полем fuel.')
        if self.frequency is not None and (not self.rail_id or self.repair_cost <= 0):
            raise ValueError('Для денежного риска нужны ось ЖД и положительная стоимость восстановления.')
        return self
