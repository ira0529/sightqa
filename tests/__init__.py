"""测试包。"""

import warnings

# 测试里会伪造 HTTPError，其内部 BytesIO 由 GC 回收，屏蔽这类噪音警告。
warnings.filterwarnings("ignore", category=ResourceWarning)
