# -*- coding: utf-8 -*-
{
    'name': 'MRP Partial Transfer to Stock',
    'version': '19.0.1.0.0',
    'category': 'Manufacturing',
    'summary': 'Transfer partial finished goods to stock while keeping the Manufacturing Order open',
    'author': 'Custom',
    'depends': ['mrp', 'stock'],
    'data': [
        'security/ir.model.access.csv',
        'wizard/mrp_partial_transfer_wizard_views.xml',
        'views/mrp_production_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
