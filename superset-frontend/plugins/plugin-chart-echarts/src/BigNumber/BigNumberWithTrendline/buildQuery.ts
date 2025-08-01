/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

import {
  buildQueryContext,
  ensureIsArray,
  getXAxisColumn,
  isXAxisSet,
  QueryFormData,
} from '@superset-ui/core';
import {
  aggregationOperator,
  flattenOperator,
  pivotOperator,
  resampleOperator,
  rollingWindowOperator,
} from '@superset-ui/chart-controls';

export default function buildQuery(formData: QueryFormData) {
  const isRawMetric = formData.aggregation === 'raw';

  const timeColumn = isXAxisSet(formData)
    ? ensureIsArray(getXAxisColumn(formData))
    : [];

  return buildQueryContext(formData, baseQueryObject => {
    const queries = [
      {
        ...baseQueryObject,
        columns: [...timeColumn],
        ...(timeColumn.length ? {} : { is_timeseries: true }),
        post_processing: [
          pivotOperator(formData, baseQueryObject),
          rollingWindowOperator(formData, baseQueryObject),
          resampleOperator(formData, baseQueryObject),
          flattenOperator(formData, baseQueryObject),
        ].filter(Boolean),
      },
    ];

    // Only add second query for raw metrics which need different query structure
    // All other aggregations (sum, mean, min, max, median, LAST_VALUE) can be computed client-side from trendline data
    if (formData.aggregation === 'raw') {
      queries.push({
        ...baseQueryObject,
        columns: [...(isRawMetric ? [] : timeColumn)],
        is_timeseries: !isRawMetric,
        post_processing: isRawMetric
          ? []
          : ([
              pivotOperator(formData, baseQueryObject),
              aggregationOperator(formData, baseQueryObject),
            ].filter(Boolean) as any[]),
      });
    }

    return queries;
  });
}
