# This file is part of the bundled PDF rendering helper.
#
# GNU AFFERO GENERAL PUBLIC LICENSE
# Version 3, 19 November 2007
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# SPDX-License-Identifier: AGPL-3.0-only

"""외부에서 복사해 온 PDF 렌더링 도우미 (검증용 예시)."""


def render_first_page(path: str) -> bytes:
    """문서 첫 페이지를 이미지로 렌더링한다."""
    import fitz  # PyMuPDF — AGPL-3.0

    with fitz.open(path) as doc:
        page = doc.load_page(0)
        return page.get_pixmap().tobytes("png")
