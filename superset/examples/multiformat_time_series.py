# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
import logging
from typing import Optional

import pandas as pd
from flask import current_app
from sqlalchemy import BigInteger, Date, DateTime, inspect, String

from superset import db
from superset.models.slice import Slice
from superset.sql.parse import Table
from superset.utils.core import DatasourceType

from ..utils.database import get_example_database  # noqa: TID252
from .helpers import (
    get_slice_json,
    get_table_connector_registry,
    merge_slice,
    misc_dash_slices,
    read_example_data,
)

logger = logging.getLogger(__name__)


def load_multiformat_time_series(  # pylint: disable=too-many-locals
    only_metadata: bool = False, force: bool = False
) -> None:
    """Loading time series data from a zip file in the repo"""
    tbl_name = "multiformat_time_series"
    database = get_example_database()
    with database.get_sqla_engine() as engine:
        schema = inspect(engine).default_schema_name
        table_exists = database.has_table(Table(tbl_name, schema))

        if not only_metadata and (not table_exists or force):
            pdf = read_example_data(
                "examples://multiformat_time_series.json.gz", compression="gzip"
            )

            # TODO(bkyryliuk): move load examples data into the pytest fixture
            if database.backend == "presto":
                pdf.ds = pd.to_datetime(pdf.ds, unit="s")
                pdf.ds = pdf.ds.dt.strftime("%Y-%m-%d")
                pdf.ds2 = pd.to_datetime(pdf.ds2, unit="s")
                pdf.ds2 = pdf.ds2.dt.strftime("%Y-%m-%d %H:%M%:%S")
            else:
                pdf.ds = pd.to_datetime(pdf.ds, unit="s")
                pdf.ds2 = pd.to_datetime(pdf.ds2, unit="s")

            pdf.to_sql(
                tbl_name,
                engine,
                schema=schema,
                if_exists="replace",
                chunksize=500,
                dtype={
                    "ds": String(255) if database.backend == "presto" else Date,
                    "ds2": String(255) if database.backend == "presto" else DateTime,
                    "epoch_s": BigInteger,
                    "epoch_ms": BigInteger,
                    "string0": String(100),
                    "string1": String(100),
                    "string2": String(100),
                    "string3": String(100),
                },
                index=False,
            )
        logger.debug("Done loading table!")
        logger.debug("-" * 80)

    logger.debug(f"Creating table [{tbl_name}] reference")
    table = get_table_connector_registry()
    obj = db.session.query(table).filter_by(table_name=tbl_name).first()
    if not obj:
        obj = table(table_name=tbl_name, schema=schema)
        db.session.add(obj)
    obj.main_dttm_col = "ds"
    obj.database = database
    obj.filter_select_enabled = True
    dttm_and_expr_dict: dict[str, tuple[Optional[str], None]] = {
        "ds": (None, None),
        "ds2": (None, None),
        "epoch_s": ("epoch_s", None),
        "epoch_ms": ("epoch_ms", None),
        "string2": ("%Y%m%d-%H%M%S", None),
        "string1": ("%Y-%m-%d^%H:%M:%S", None),
        "string0": ("%Y-%m-%d %H:%M:%S.%f", None),
        "string3": ("%Y/%m/%d%H:%M:%S.%f", None),
    }
    for col in obj.columns:
        dttm_and_expr = dttm_and_expr_dict[col.column_name]
        col.python_date_format = dttm_and_expr[0]
        col.database_expression = dttm_and_expr[1]
        col.is_dttm = True
    obj.fetch_metadata()
    tbl = obj

    logger.debug("Creating Heatmap charts")
    for i, col in enumerate(tbl.columns):
        slice_data = {
            "metrics": ["count"],
            "granularity_sqla": col.column_name,
            "row_limit": current_app.config["ROW_LIMIT"],
            "since": "2015",
            "until": "2016",
            "viz_type": "cal_heatmap",
            "domain_granularity": "month",
            "subdomain_granularity": "day",
        }

        slc = Slice(
            slice_name=f"Calendar Heatmap multiformat {i}",
            viz_type="cal_heatmap",
            datasource_type=DatasourceType.TABLE,
            datasource_id=tbl.id,
            params=get_slice_json(slice_data),
        )
        merge_slice(slc)
    misc_dash_slices.add("Calendar Heatmap multiformat 0")
