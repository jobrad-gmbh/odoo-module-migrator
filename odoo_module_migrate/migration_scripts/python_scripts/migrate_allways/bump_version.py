def bump_revision(**kwargs):
    tools = kwargs["tools"]
    manifest_path = kwargs["manifest_path"]
    migration_steps = kwargs["migration_steps"]
    target_version_name = migration_steps[-1]["target_version_name"]

    new_version = "%s.1.0.0" % target_version_name

    old_term = r"(?P<lq>'|\")version(?P<rq>'|\").*('|\").*('|\")"
    new_term = r'\g<lq>version\g<rq>: \g<lq>{0}\g<rq>'.format(new_version)
    tools._replace_in_file(
        manifest_path, {old_term: new_term}, "Bump version to %s" % new_version
    )
