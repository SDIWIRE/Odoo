{
    'name': 'SDI Work Center Concurrency',
    'version': '19.0.1.0.1',
    'category': 'Manufacturing',
    'summary': 'Decouple work center scheduling concurrency from product-capacity duration math',
    'description': """
Adds a "Concurrent Operators" field to Work Centers, independent of the existing
per-product Capacity field on the Product Capacities tab.

Capacity continues to mean "units processed together in one cycle" and keeps
driving duration_expected exactly as before. Concurrent Operators controls a
separate thing: how many work orders the scheduler is allowed to place in
overlapping time windows on one work center record, for stations staffed by
multiple people each building separate units sequentially.

Work centers not using this (Concurrent Operators = 1, the default) are
unaffected - scheduling and duration math behave exactly as in stock mrp.
""",
    'depends': ['mrp'],
    'data': [
        'views/mrp_workcenter_views.xml',
    ],
    'author': 'iCONN Systems / SDI Wire & Cable',
    'license': 'LGPL-3',
    'application': False,
    'installable': True,
}
