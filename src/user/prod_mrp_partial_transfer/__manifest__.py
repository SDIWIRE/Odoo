# -*- coding: utf-8 -*-
{
    'name': 'MRP Partial Transfer to Stock',
    'version': '19.0.2.2.0',
    'category': 'Manufacturing',
    'summary': 'Post partial production on the MO itself and put it away in one step',
    'description': """
Partial transfers are posted against the Manufacturing Order through Odoo's own
production posting, so one stock movement per unit drives on-hand, forecast,
component consumption and cost. The chained storage step is validated for the
posted quantity, and the final transfer closes the MO with the standard
Mark as Done. Replaces 19.0.1.0.0, which posted a separate receipt that was not
linked to the MO, and converts the receipts that version made. See README.md.
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
