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
"""change_fetch_values_predicate_to_text

Revision ID: 07071313dd52
Revises: 6d20ba9ecb33
Create Date: 2021-08-09 17:32:56.204184

"""

# revision identifiers, used by Alembic.
revision = "07071313dd52"
down_revision = "6d20ba9ecb33"

import logging  # noqa: E402

import sqlalchemy as sa  # noqa: E402
from alembic import op  # noqa: E402
from sqlalchemy import func  # noqa: E402

from superset import db  # noqa: E402
from superset.connectors.sqla.models import SqlaTable  # noqa: E402

logger = logging.getLogger("alembic.env")


def upgrade():
    with op.batch_alter_table("tables") as batch_op:
        batch_op.alter_column(
            "fetch_values_predicate",
            existing_type=sa.String(length=1000),
            type_=sa.Text(),
            existing_nullable=True,
        )


def remove_value_if_too_long():
    bind = op.get_bind()
    session = db.Session(bind=bind)

    # it will be easier for users to notice that their field has been deleted rather than truncated  # noqa: E501
    # so just remove it if it won't fit back into the 1000 string length column
    try:
        rows = (
            session.query(SqlaTable)
            .filter(func.length(SqlaTable.fetch_values_predicate) > 1000)
            .all()
        )

        for row in rows:
            row.fetch_values_predicate = None

        logger.info("%d values deleted", len(rows))

        session.commit()
        session.close()
    except Exception as ex:
        logger.warning(ex)


def downgrade():
    remove_value_if_too_long()

    with op.batch_alter_table("tables") as batch_op:
        batch_op.alter_column(
            "fetch_values_predicate",
            existing_type=sa.Text(),
            type_=sa.String(length=1000),
            existing_nullable=True,
        )
