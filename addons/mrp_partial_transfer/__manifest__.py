# -*- coding: utf-8 -*-
{
    'name': 'MRP Partial Transfer to Stock',
    'version': '19.0.1.0.0',
    'category': 'Manufacturing',
    'summary': 'Transfer partial finished goods to stock while keeping the Manufacturing Order open',
    'description': """
        Adds a "Transfer to Stock" button on confirmed Manufacturing Orders.
        Allows operators to move a partial quantity of finished goods into
        inventory without closing the MO. The MO remains In Progress and
        can be partially transferred multiple times until the full demand
        is met, at which point the standard Validate flow closes it.
    """,
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
