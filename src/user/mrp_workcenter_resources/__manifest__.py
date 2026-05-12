{
    'name': 'Work Center Parallel Scheduling',
    'summary': 'Set the number of workers per work center for parallel job scheduling',
    'description': """
        Extends Odoo's MRP work centers to support parallel job scheduling.

        When a work center has multiple workers who each run independent jobs
        simultaneously, set 'Number of Workers' to the correct count. The module
        creates one resource.resource slot per worker and integrates with the MRP
        scheduler so that up to N work orders can be planned concurrently on the
        same work center.
    """,
    'version': '19.0.1.0.0',
    'category': 'Manufacturing',
    'author': 'SDI Wire & Cable',
    'license': 'LGPL-3',
    'depends': ['mrp', 'mrp_workorder'],
    'data': [
        'security/ir.model.access.csv',
        'views/mrp_workcenter_resource_views.xml',
        'views/mrp_workcenter_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
