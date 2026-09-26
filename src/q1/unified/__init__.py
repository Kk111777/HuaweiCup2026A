"""Serial unified Q1 solver: api.solve_case shares one budget across core counts.

api.solve_file remains the single-target wrapper. Neither requires a singlecore
baseline; optional speedup denominators belong to offline reporting only.
"""
