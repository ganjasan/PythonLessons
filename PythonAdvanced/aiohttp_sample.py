import io
import aiohttp
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List
import pandas as pd
import aioboto3
from aiobotocore.session import get_session
import json
import itertools

import requests
from cachetools.func import ttl_cache
from itertools import islice

from loguru import logger

MAX_TILES_IN_REQUEST = 2
AUTH_TOKEN_EXPIRES_IN = 86400
USER = 'Rr5f2TPuwALNZrJ8PpQjPB0zuliwx0Lc'
PASSWORD = 'ipupGmqkeFBI5OFR'


def chunk(it, size):
    it = iter(it)
    return iter(lambda: tuple(islice(it, size)), ())


@ttl_cache(maxsize=10, ttl=AUTH_TOKEN_EXPIRES_IN)
def get_auth_token(usr, pwd):
    url = "https://consent.swisscom.com/o/oauth2/token"
    data = {"grant_type": "client_credentials"}
    response = requests.post(url, data=data, auth=requests.auth.HTTPBasicAuth(
        usr, pwd))
    resp_json = response.json()
    access_token = resp_json.get('access_token')
    return access_token


def get_headers():
    auth_token = get_auth_token(USER, PASSWORD)
    headers = {
        "SCS-Version": "2",
        "Authorization": f"Bearer {auth_token}"
    }
    return headers


def get_tiles_param_string(tiles_id_list):
    return "&".join([f"tiles={tile_id}" for tile_id in tiles_id_list])


def get_date_range(start_dt:datetime, end_dt:datetime):
    if end_dt < start_dt:
        raise ValueError("start_dt can`t be less than end_dt")
    
    hours_diff = int((end_dt-start_dt).total_seconds()//3600)
    for x in range(hours_diff):
        yield start_dt+timedelta(hours=x)


async def start_requests(session:aiohttp.ClientSession, start_dt:datetime, end_dt:datetime, tiles: List[int]):
    tiles = set(tiles)
    date_range = get_date_range(start_dt, end_dt)
    
    tasks = []
    boto_session = get_session()
    for dt in date_range:
        task = asyncio.create_task(collect_and_save_hour_dwell_density(aiohttp_session=session, boto_sesion=boto_session, dt=dt, tiles=tiles))
        tasks.append(task)
    
    await asyncio.gather(*tasks)


async def collect_and_save_hour_dwell_density(aiohttp_session, boto_sesion, dt, tiles):
    results = await collect_hour_dwell_density(aiohttp_session, dt, tiles)
    result_data = list(itertools.chain.from_iterable([result for result in results if result is not None]))
    

    await save_hour_dwell_density(boto_session=boto_sesion, dt=dt, data=result_data)

async def collect_hour_dwell_density(session:aiohttp.ClientSession, dt:datetime, tiles:List[int]):
    tasks = []
    for tiles_chunk in chunk(tiles, MAX_TILES_IN_REQUEST):
        task = asyncio.create_task(get_hourly_dwell_density(session, dt, tiles_chunk))
        tasks.append(task)
    results = await asyncio.gather(*tasks)
    return results


async def save_data_to_s3_in_parquet(boto_session, data: List[Dict], file_path:str):
    out_buffer = io.BytesIO()
    df = pd.DataFrame(data)
    df.to_parquet(out_buffer, index=False)

    bucket = "swip-test-bucket"

    async with boto_session.create_client('s3') as s3:
        await s3.put_object(Bucket=bucket, Key=file_path, Body=out_buffer.getvalue())


async def save_hour_dwell_density(boto_session, dt:datetime, data:List[Dict]):
    dt_str = dt.isoformat(timespec='minutes')
    path = f"swisscom_dwell_density/dt={dt_str}/{datetime.now().timestamp()}.parquet"

    await save_data_to_s3_in_parquet(boto_session=boto_session, data=data, file_path=path)


async def get_hourly_dwell_density(session:aiohttp.ClientSession, dt:datetime, tiles:List[int]):
    if len(tiles)>100:
        raise ValueError("tiles count cant be more than 100")

    iso_dt = dt.isoformat(timespec='minutes')
    tiles_param_string = get_tiles_param_string(tiles)
    url = f"https://api.swisscom.com/layer/heatmaps/standard/heatmaps/dwell-density/hourly/{iso_dt}?{tiles_param_string}"
    async with session.get(url) as r:
        json_body = await r.json()
        if r.status != 200:
            logger.warning(json.dumps(json_body))
            return

        result_tiles = json_body.get('tiles')
        for tile in result_tiles:
            tile['datetime'] = dt
        
        return result_tiles

async def main():
    start_dt = datetime.fromisoformat("2022-08-15T00:00")
    end_dt = datetime.fromisoformat("2022-08-15T02:00")
    tiles = [31678799, 31726578, 31790330]
    
    headers = get_headers()
    async with aiohttp.ClientSession(headers=headers) as session:
        await start_requests(session, start_dt, end_dt, tiles)


if __name__ == "__main__":
    asyncio.run(main())