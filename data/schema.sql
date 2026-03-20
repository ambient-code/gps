CREATE INDEX idx_changelog_changed_at ON issue_changelog(changed_at);
CREATE INDEX idx_changelog_field ON issue_changelog(field);
CREATE INDEX idx_changelog_issue ON issue_changelog(issue_id);
CREATE INDEX idx_feature_component_fid ON feature_component(feature_id);
CREATE INDEX idx_feature_issue_key ON feature(issue_key);
CREATE INDEX idx_feature_release_fid ON feature_release(feature_id);
CREATE INDEX idx_feature_team_fid ON feature_team(feature_id);
CREATE INDEX idx_governance_doc_type ON governance_document(doc_type);
CREATE INDEX idx_issue_component_name ON issue_component(component_name);
CREATE INDEX idx_issue_label_label ON issue_label(label);
CREATE INDEX idx_jira_issue_assignee ON jira_issue(assignee);
CREATE INDEX idx_jira_issue_priority ON jira_issue(priority);
CREATE INDEX idx_jira_issue_status ON jira_issue(status);
CREATE INDEX idx_jira_issue_updated ON jira_issue(updated);
CREATE INDEX idx_person_email ON person(email);
CREATE INDEX idx_person_name ON person(name);
CREATE INDEX idx_person_user_id ON person(user_id);
CREATE INDEX idx_release_milestone_version ON release_milestone(version);
CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE component_version_map (
    component TEXT NOT NULL, release_version TEXT NOT NULL,
    component_version TEXT, component_group TEXT,
    PRIMARY KEY (component, release_version)
);
CREATE TABLE feature (
    feature_id INTEGER PRIMARY KEY, issue_key TEXT UNIQUE NOT NULL,
    title TEXT, project TEXT, hierarchy TEXT,
    assignee TEXT, sprint TEXT,
    target_start TEXT, target_end TEXT, due_date TEXT,
    estimates_days REAL, parent_key TEXT, priority TEXT,
    issue_status TEXT, progress_pct REAL,
    progress_completed_days REAL, progress_remaining_days REAL,
    progress_pct_ic REAL, todo_ic INTEGER, in_progress_ic INTEGER,
    done_ic INTEGER, total_ic INTEGER, color_status TEXT,
    release_date TEXT, rice_score REAL,
    dev_approval TEXT, developer TEXT, docs_approval TEXT,
    docs_impact TEXT, product_lead TEXT, product_manager TEXT,
    qe_approval TEXT, tester TEXT, owner TEXT, architect TEXT,
    plm_tech_lead TEXT, tech_lead TEXT, target_milestone TEXT
);
CREATE TABLE feature_component (
    feature_id INTEGER REFERENCES feature(feature_id),
    component TEXT NOT NULL, PRIMARY KEY (feature_id, component)
);
CREATE TABLE feature_label (
    feature_id INTEGER REFERENCES feature(feature_id),
    label TEXT NOT NULL, PRIMARY KEY (feature_id, label)
);
CREATE TABLE feature_release (
    feature_id INTEGER REFERENCES feature(feature_id),
    release TEXT NOT NULL, PRIMARY KEY (feature_id, release)
);
CREATE TABLE feature_team (
    feature_id INTEGER REFERENCES feature(feature_id),
    team TEXT NOT NULL, PRIMARY KEY (feature_id, team)
);
CREATE TABLE governance_document (
    doc_id INTEGER PRIMARY KEY,
    doc_type TEXT NOT NULL CHECK(doc_type IN ('constitution','policy','standard','reference')),
    title TEXT NOT NULL,
    version TEXT,
    category TEXT,
    content TEXT,
    sections TEXT,  -- JSON: [{heading, level, content}]
    source_file TEXT,
    source_url TEXT,
    extracted_at TEXT,
    hash TEXT
);
CREATE TABLE issue_changelog (
    changelog_id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES jira_issue(issue_id),
    field TEXT NOT NULL,
    field_type TEXT,
    from_value TEXT,
    to_value TEXT,
    author TEXT,
    changed_at TEXT NOT NULL
);
CREATE TABLE issue_component (
    issue_id INTEGER REFERENCES jira_issue(issue_id),
    component_name TEXT NOT NULL, PRIMARY KEY (issue_id, component_name)
);
CREATE TABLE issue_label (
    issue_id INTEGER REFERENCES jira_issue(issue_id),
    label TEXT NOT NULL, PRIMARY KEY (issue_id, label)
);
CREATE TABLE jira_component (
    component_id INTEGER PRIMARY KEY, component_name TEXT UNIQUE NOT NULL
);
CREATE TABLE jira_issue (
    issue_id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL,
    summary TEXT, status TEXT, priority TEXT,
    assignee TEXT, reporter TEXT, issue_type TEXT,
    created TEXT, updated TEXT
);
CREATE TABLE jira_scrum_mapping (
    component_name TEXT NOT NULL, scrum_team TEXT, specialty TEXT
);
CREATE TABLE miro_team (
    miro_team_id INTEGER PRIMARY KEY, miro_team_name TEXT UNIQUE NOT NULL
);
CREATE TABLE org (
    org_id INTEGER PRIMARY KEY, org_key TEXT UNIQUE NOT NULL,
    org_name TEXT NOT NULL, tab_name TEXT NOT NULL
);
CREATE TABLE org_chart_raw (
    line_num INTEGER PRIMARY KEY, content TEXT NOT NULL
);
CREATE TABLE person (
    person_id INTEGER PRIMARY KEY, name TEXT NOT NULL,
    manager TEXT, org_id INTEGER REFERENCES org(org_id),
    specialty_id INTEGER REFERENCES specialty(specialty_id),
    status TEXT, last_modified TEXT,
    user_id TEXT, job_title TEXT, email TEXT, location TEXT, manager_uid TEXT,
    source TEXT NOT NULL DEFAULT 'spreadsheet',
    UNIQUE(name, org_id)
);
CREATE TABLE person_component (
    person_id INTEGER REFERENCES person(person_id),
    component_id INTEGER REFERENCES jira_component(component_id),
    fte_fraction REAL NOT NULL,
    PRIMARY KEY (person_id, component_id)
);
CREATE TABLE person_miro_team (
    person_id INTEGER REFERENCES person(person_id),
    miro_team_id INTEGER REFERENCES miro_team(miro_team_id),
    PRIMARY KEY (person_id, miro_team_id)
);
CREATE TABLE person_scrum_team (
    person_id INTEGER REFERENCES person(person_id),
    team_id INTEGER REFERENCES scrum_team(team_id),
    PRIMARY KEY (person_id, team_id)
);
CREATE TABLE release_milestone (
    product TEXT NOT NULL, version TEXT NOT NULL,
    event_type TEXT NOT NULL, event_date TEXT,
    PRIMARY KEY (product, version, event_type)
);
CREATE TABLE release_schedule (
    schedule_id INTEGER PRIMARY KEY,
    release TEXT NOT NULL, task TEXT NOT NULL,
    date_start TEXT, date_finish TEXT
);
CREATE TABLE scrum_team (
    team_id INTEGER PRIMARY KEY, team_name TEXT UNIQUE NOT NULL,
    pm TEXT, eng_lead TEXT
);
CREATE TABLE scrum_team_board (
    id INTEGER PRIMARY KEY,
    organization TEXT,
    scrum_team_name TEXT NOT NULL,
    jira_board_url TEXT,
    pm TEXT,
    agilist REAL,
    architects REAL,
    bff REAL,
    backend_engineer REAL,
    devops REAL,
    manager REAL,
    operations_manager REAL,
    qe REAL,
    staff_engineers REAL,
    ui REAL,
    total_staff REAL
);
CREATE TABLE specialty (
    specialty_id INTEGER PRIMARY KEY, specialty_name TEXT UNIQUE NOT NULL
);
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE sqlite_stat1(tbl,idx,stat);
CREATE VIEW v_component_headcount AS
SELECT jc.component_name, COUNT(pc.person_id) AS headcount,
       ROUND(SUM(pc.fte_fraction), 1) AS fte_total
FROM jira_component jc
LEFT JOIN person_component pc ON jc.component_id = pc.component_id
GROUP BY jc.component_id ORDER BY fte_total DESC;
CREATE VIEW v_feature_summary AS
SELECT issue_status, COUNT(*) AS cnt, ROUND(AVG(rice_score),1) AS avg_rice
FROM feature GROUP BY issue_status ORDER BY cnt DESC;
CREATE VIEW v_governance_toc AS
SELECT doc_id, doc_type, title, version, category, source_file,
       json_group_array(json_object(
           'heading', json_extract(value, '$.heading'),
           'level', json_extract(value, '$.level')
       )) AS table_of_contents
FROM governance_document, json_each(sections)
GROUP BY doc_id;
CREATE VIEW v_issue_component_summary AS
SELECT ic.component_name, COUNT(*) AS issue_count,
       SUM(CASE WHEN ji.status IN ('Resolved','Closed','Done') THEN 1 ELSE 0 END) AS done_count
FROM jira_issue ji JOIN issue_component ic USING(issue_id)
GROUP BY ic.component_name ORDER BY issue_count DESC;
CREATE VIEW v_person_detail AS
SELECT
    p.person_id, p.name, p.user_id, p.manager, p.manager_uid,
    o.org_name, o.org_key, s.specialty_name AS specialty,
    p.job_title, p.email, p.location, p.status, p.source, p.last_modified,
    GROUP_CONCAT(DISTINCT jc.component_name) AS components,
    GROUP_CONCAT(DISTINCT mt.miro_team_name) AS miro_teams,
    GROUP_CONCAT(DISTINCT st.team_name) AS scrum_teams
FROM person p
LEFT JOIN org o ON p.org_id = o.org_id
LEFT JOIN specialty s ON p.specialty_id = s.specialty_id
LEFT JOIN person_component pc ON p.person_id = pc.person_id
LEFT JOIN jira_component jc ON pc.component_id = jc.component_id
LEFT JOIN person_miro_team pmt ON p.person_id = pmt.person_id
LEFT JOIN miro_team mt ON pmt.miro_team_id = mt.miro_team_id
LEFT JOIN person_scrum_team pst ON p.person_id = pst.person_id
LEFT JOIN scrum_team st ON pst.team_id = st.team_id
GROUP BY p.person_id;
CREATE VIEW v_team_headcount AS
SELECT st.team_name, COUNT(pst.person_id) AS headcount
FROM scrum_team st
LEFT JOIN person_scrum_team pst ON st.team_id = pst.team_id
GROUP BY st.team_id ORDER BY headcount DESC;
CREATE VIEW v_unassigned AS
SELECT p.person_id, p.name, p.user_id, p.job_title, p.email,
       p.location, p.manager_uid
FROM person p
WHERE p.person_id NOT IN (SELECT person_id FROM person_scrum_team)
  AND p.person_id NOT IN (SELECT person_id FROM person_component)
ORDER BY p.name;
