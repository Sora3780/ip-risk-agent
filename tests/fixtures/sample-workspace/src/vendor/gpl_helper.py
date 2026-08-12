# This file is part of the Nova Dashboard vendored utilities.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""외부 프로젝트에서 가져온 시계열 리샘플링 헬퍼."""


def resample(points, bucket_seconds):
    """(timestamp, value) 목록을 고정 간격 버킷으로 묶어 평균을 낸다."""
    if not points:
        return []
    buckets = {}
    for ts, value in points:
        key = int(ts) // bucket_seconds * bucket_seconds
        buckets.setdefault(key, []).append(value)
    return [(k, sum(v) / len(v)) for k, v in sorted(buckets.items())]


def fill_gaps(series, bucket_seconds, default=0.0):
    """비어 있는 버킷을 default 로 채운다."""
    if not series:
        return []
    out = []
    start, end = series[0][0], series[-1][0]
    lookup = dict(series)
    for key in range(start, end + bucket_seconds, bucket_seconds):
        out.append((key, lookup.get(key, default)))
    return out
