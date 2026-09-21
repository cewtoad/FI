"""Synthesize a fake F1 UDP packet stream for offline testing.

Uses the packet classes' own from_values()/to_bytes() so the bytes we generate
are exactly what the real parser expects.
"""

from __future__ import annotations

from typing import List

from lib.f1_types import (ActualTyreCompound, F1PacketType, PacketCarStatusData,
                          PacketCarTelemetryData, PacketHeader, PacketLapData,
                          PacketSessionData, CarStatusData, CarTelemetryData,
                          LapData, TractionControlAssistMode)

PACKET_FORMAT = 2025
NUM_CARS = 22
PLAYER = 0


def _header(packet_id: F1PacketType, session_uid: int, frame: int,
            session_time: float) -> PacketHeader:
    return PacketHeader.from_values(
        packet_format=PACKET_FORMAT,
        game_year=25,
        game_major_version=1,
        game_minor_version=0,
        packet_version=1,
        packet_type=packet_id,
        session_uid=session_uid,
        session_time=session_time,
        frame_identifier=frame,
        overall_frame_identifier=frame,
        player_car_index=PLAYER,
        secondary_player_car_index=255,
    )


def make_session(session_uid: int, total_laps: int) -> bytes:
    # Only a few fields matter to us; fill the rest with defaults via from_values.
    import inspect
    sig = inspect.signature(PacketSessionData.from_values)
    kwargs = {}
    for name in sig.parameters:
        if name in ("cls",):
            continue
        p = sig.parameters[name]
        if p.default is not inspect.Parameter.empty:
            continue
        # crude defaults by name
        if "session_type" in name:
            kwargs[name] = 15  # race-ish; enum safeCast tolerant
        elif "track" in name:
            kwargs[name] = 0
        elif "total_laps" in name:
            kwargs[name] = total_laps
        else:
            kwargs[name] = 0
    # known required ones
    kwargs.setdefault("total_laps", total_laps)
    pkt = PacketSessionData.from_values(_header(F1PacketType.SESSION, session_uid, 0, 0.0), **kwargs)
    return pkt.to_bytes()


def make_lap(packet_format: int, packet_id: F1PacketType, session_uid: int, frame: int,
             session_time: float, last_lap_ms: int, cur_lap_ms: int,
             lap_distance: float, total_distance: float,
             cur_lap_num: int, position: int, sector: int,
             current_lap_invalid: int = 0) -> bytes:
    laps: List[LapData] = []
    for i in range(NUM_CARS):
        if i == PLAYER:
            laps.append(LapData.from_values(
                last_lap_time_ms=last_lap_ms,
                current_lap_time_ms=cur_lap_ms,
                sector1_time_ms=cur_lap_ms // 3 if sector > 0 else 0,
                sector1_time_minutes=0,
                sector2_time_ms=cur_lap_ms // 3 if sector > 1 else 0,
                sector2_time_minutes=0,
                delta_to_front_ms=1200,
                delta_to_front_minutes=0,
                delta_to_leader_ms=5400,
                delta_to_leader_minutes=0,
                lap_distance=lap_distance,
                total_distance=total_distance,
                safety_car_delta=0.0,
                car_position=position,
                current_lap_num=cur_lap_num,
                pit_status=0,
                num_pit_stops=0,
                sector=sector,
                current_lap_invalid=current_lap_invalid,
                penalties=0,
                total_warnings=0,
                corner_cutting_warnings=0,
                num_unserved_drive_through_pens=0,
                num_unserved_stop_go_pens=0,
                grid_position=position,
                driver_status=4,
                result_status=2,
                pit_lane_timer_active=0,
                pit_lane_time_ms=0,
                pit_stop_timer_ms=0,
                pit_stop_should_serve_pen=0,
                speed_trap_fastest_speed=310.0,
                speed_trap_fastest_lap=1,
                packet_format=packet_format,
            ))
        else:
            laps.append(_empty_lap(laps[0], i))
    pkt = PacketLapData.from_values(_header(packet_id, session_uid, frame, session_time), laps)
    return pkt.to_bytes()


def _empty_lap(template: LapData, idx: int) -> LapData:
    # reuse template's bytes but zero out the position/lap to distinguish cars
    d = bytearray(template.to_bytes())
    return LapData(bytes(d), template.m_packetFormat)


def make_telemetry(packet_format: int, session_uid: int, frame: int, session_time: float,
                   speed: int, gear: int, throttle: float, brake: float,
                   tyre_surface: int) -> bytes:
    cars: List[CarTelemetryData] = []
    for i in range(NUM_CARS):
        cars.append(CarTelemetryData.from_values(
            speed=speed if i == PLAYER else 0,
            throttle=int(throttle * 100) if i == PLAYER else 0,
            steer=0,
            brake=int(brake * 100) if i == PLAYER else 0,
            clutch=0,
            gear=gear if i == PLAYER else 0,
            engine_rpm=11000,
            drs=0,
            rev_lights_percent=0,
            rev_lights_bit_value=0,
            brakes_temperature_0=350,
            brakes_temperature_1=350,
            brakes_temperature_2=300,
            brakes_temperature_3=300,
            tyres_surface_temperature_0=tyre_surface if i == PLAYER else 40,
            tyres_surface_temperature_1=tyre_surface if i == PLAYER else 40,
            tyres_surface_temperature_2=tyre_surface if i == PLAYER else 40,
            tyres_surface_temperature_3=tyre_surface if i == PLAYER else 40,
            tyres_inner_temperature_0=100,
            tyres_inner_temperature_1=100,
            tyres_inner_temperature_2=100,
            tyres_inner_temperature_3=100,
            engine_temperature=105,
            tyres_pressure_0=22.5,
            tyres_pressure_1=22.5,
            tyres_pressure_2=21.5,
            tyres_pressure_3=21.5,
            surface_type_0=0,
            surface_type_1=0,
            surface_type_2=0,
            surface_type_3=0,
            packet_format=packet_format,
        ))
    pkt = PacketCarTelemetryData.from_values(
        _header(F1PacketType.CAR_TELEMETRY, session_uid, frame, session_time), cars,
        mfd_panel_index=0, mfd_panel_index_secondary_player=0, suggested_gear=0)
    return pkt.to_bytes()


def make_status(packet_format: int, session_uid: int, frame: int, session_time: float,
                fuel_kg: float, fuel_laps: float, tyre_age: int) -> bytes:
    cars: List[CarStatusData] = []
    for i in range(NUM_CARS):
        cars.append(CarStatusData.from_values(
            traction_control=TractionControlAssistMode.OFF,
            anti_lock_brakes=True,
            fuel_mix=CarStatusData.FuelMix.STANDARD,
            front_brake_bias=56,
            pit_limiter_status=False,
            fuel_in_tank=fuel_kg if i == PLAYER else 50.0,
            fuel_capacity=110.0,
            fuel_remaining_laps=fuel_laps if i == PLAYER else 10.0,
            max_rpm=15000,
            idle_rpm=4000,
            max_gears=8,
            drs_allowed=1,
            drs_activation_distance=0,
            actual_tyre_compound=ActualTyreCompound.MEDIUM,
            visual_tyre_compound=ActualTyreCompound.MEDIUM,
            tyres_age_laps=tyre_age if i == PLAYER else 0,
            m_vehicle_fia_flags=CarStatusData.VehicleFIAFlags.NONE,
            engine_power_ice=850.0,
            engine_power_mguk=120.0,
            ers_store_energy=4_000_000.0,
            ers_deploy_mode=CarStatusData.ERSDeployMode.MEDIUM,
            ers_harvested_this_lap_mguk=0.0,
            ers_harvested_this_lap_mguh=0.0,
            ers_deployed_this_lap=0.0,
            network_paused=0,
            packet_format=packet_format,
        ))
    pkt = PacketCarStatusData.from_values(
        _header(F1PacketType.CAR_STATUS, session_uid, frame, session_time), cars)
    return pkt.to_bytes()
