#!/usr/bin/env python3
"""Generate sample SQLite database for the demo."""

import sqlite3
import random
from datetime import date, timedelta
from pathlib import Path

# Sample data
DEPARTMENTS = [
    ("Engineering", 2500000),
    ("Sales", 1800000),
    ("Marketing", 1200000),
    ("Human Resources", 800000),
    ("Finance", 1000000),
    ("Operations", 1500000),
]

FIRST_NAMES = [
    "Alice",
    "Bob",
    "Charlie",
    "Diana",
    "Eve",
    "Frank",
    "Grace",
    "Henry",
    "Ivy",
    "Jack",
    "Karen",
    "Leo",
    "Maria",
    "Nathan",
    "Olivia",
    "Paul",
    "Quinn",
    "Rachel",
    "Sam",
    "Tina",
    "Uma",
    "Victor",
    "Wendy",
    "Xavier",
    "Yuki",
    "Zach",
    "Aria",
    "Blake",
    "Cora",
    "Derek",
    "Emma",
    "Felix",
]

LAST_NAMES = [
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Rodriguez",
    "Martinez",
    "Hernandez",
    "Lopez",
    "Gonzalez",
    "Wilson",
    "Anderson",
    "Thomas",
    "Taylor",
    "Moore",
    "Jackson",
    "Martin",
    "Lee",
    "Perez",
    "Thompson",
    "White",
    "Harris",
    "Sanchez",
    "Clark",
    "Ramirez",
    "Lewis",
    "Robinson",
    "Walker",
    "Young",
    "Allen",
    "King",
]

PROJECT_NAMES = [
    "Platform Modernization",
    "Customer Portal V2",
    "Data Pipeline Rebuild",
    "Mobile App Launch",
    "API Gateway",
    "Security Audit",
    "Cloud Migration",
    "Analytics Dashboard",
    "CRM Integration",
    "Automation Framework",
    "DevOps Pipeline",
    "Performance Optimization",
]

PROJECT_STATUSES = ["planning", "active", "on_hold", "completed"]


def create_database(db_path: Path) -> None:
    """Create and populate the sample database."""
    # Remove existing database
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Create tables
    cursor.executescript("""
        -- departments
        CREATE TABLE departments (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            budget REAL NOT NULL
        );

        -- employees
        CREATE TABLE employees (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            department_id INTEGER NOT NULL,
            hire_date DATE NOT NULL,
            salary REAL NOT NULL,
            FOREIGN KEY (department_id) REFERENCES departments(id)
        );

        -- projects
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            department_id INTEGER NOT NULL,
            start_date DATE NOT NULL,
            end_date DATE,
            status TEXT NOT NULL,
            FOREIGN KEY (department_id) REFERENCES departments(id)
        );

        -- timesheets
        CREATE TABLE timesheets (
            id INTEGER PRIMARY KEY,
            employee_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL,
            date DATE NOT NULL,
            hours REAL NOT NULL,
            FOREIGN KEY (employee_id) REFERENCES employees(id),
            FOREIGN KEY (project_id) REFERENCES projects(id)
        );

        -- Create indexes for common queries
        CREATE INDEX idx_employees_department ON employees(department_id);
        CREATE INDEX idx_projects_department ON projects(department_id);
        CREATE INDEX idx_projects_status ON projects(status);
        CREATE INDEX idx_timesheets_employee ON timesheets(employee_id);
        CREATE INDEX idx_timesheets_project ON timesheets(project_id);
        CREATE INDEX idx_timesheets_date ON timesheets(date);
    """)

    # Insert departments
    for i, (name, budget) in enumerate(DEPARTMENTS, 1):
        cursor.execute(
            "INSERT INTO departments (id, name, budget) VALUES (?, ?, ?)",
            (i, name, budget),
        )

    # Generate employees (~150 total, distributed across departments)
    employee_id = 1
    employees_by_dept: dict[int, list[int]] = {i: [] for i in range(1, len(DEPARTMENTS) + 1)}
    base_date = date(2020, 1, 1)

    for dept_id in range(1, len(DEPARTMENTS) + 1):
        # More employees in Engineering and Sales
        num_employees = random.randint(20, 35) if dept_id <= 2 else random.randint(15, 25)

        for _ in range(num_employees):
            first = random.choice(FIRST_NAMES)
            last = random.choice(LAST_NAMES)
            name = f"{first} {last}"
            email = f"{first.lower()}.{last.lower()}{employee_id}@company.com"
            hire_date = base_date + timedelta(days=random.randint(0, 1500))
            # Salary range varies by department
            base_salary = 60000 + (dept_id * 5000)
            salary = base_salary + random.randint(0, 50000)

            cursor.execute(
                """INSERT INTO employees (id, name, email, department_id, hire_date, salary)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (employee_id, name, email, dept_id, hire_date.isoformat(), salary),
            )
            employees_by_dept[dept_id].append(employee_id)
            employee_id += 1

    # Generate projects
    project_id = 1
    projects_by_dept: dict[int, list[int]] = {i: [] for i in range(1, len(DEPARTMENTS) + 1)}

    for name in PROJECT_NAMES:
        dept_id = random.randint(1, len(DEPARTMENTS))
        start = base_date + timedelta(days=random.randint(0, 1000))
        status = random.choice(PROJECT_STATUSES)
        end_date = None
        if status == "completed":
            end_date = start + timedelta(days=random.randint(60, 365))
        elif status != "planning":
            # Active/on_hold might have estimated end date
            if random.random() > 0.3:
                end_date = start + timedelta(days=random.randint(90, 540))

        cursor.execute(
            """INSERT INTO projects (id, name, department_id, start_date, end_date, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                name,
                dept_id,
                start.isoformat(),
                end_date.isoformat() if end_date else None,
                status,
            ),
        )
        projects_by_dept[dept_id].append(project_id)
        project_id += 1

    # Generate timesheets (many rows for realistic queries)
    timesheet_id = 1
    timesheet_start = date(2024, 1, 1)
    timesheet_end = date(2024, 12, 31)

    # Each employee logs time ~3 days/week on average
    all_employees = list(range(1, employee_id))

    current_date = timesheet_start
    while current_date <= timesheet_end:
        # Skip weekends
        if current_date.weekday() >= 5:
            current_date += timedelta(days=1)
            continue

        # ~60% of employees log time each day
        working_today = random.sample(all_employees, int(len(all_employees) * 0.6))

        for emp_id in working_today:
            # Find employee's department and available projects
            for dept_id, emp_list in employees_by_dept.items():
                if emp_id in emp_list:
                    # Employee works on projects in their department (mostly)
                    available_projects = projects_by_dept[dept_id].copy()
                    # Sometimes cross-department collaboration
                    if random.random() > 0.8:
                        other_dept = random.randint(1, len(DEPARTMENTS))
                        if projects_by_dept[other_dept]:
                            available_projects.extend(projects_by_dept[other_dept])

                    if available_projects:
                        proj_id = random.choice(available_projects)
                        hours = random.choice([4.0, 6.0, 7.5, 8.0, 8.0, 8.0])

                        cursor.execute(
                            """INSERT INTO timesheets (id, employee_id, project_id, date, hours)
                               VALUES (?, ?, ?, ?, ?)""",
                            (timesheet_id, emp_id, proj_id, current_date.isoformat(), hours),
                        )
                        timesheet_id += 1
                    break

        current_date += timedelta(days=1)

    conn.commit()

    # Print summary
    cursor.execute("SELECT COUNT(*) FROM departments")
    dept_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM employees")
    emp_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM projects")
    proj_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM timesheets")
    ts_count = cursor.fetchone()[0]

    print(f"Created database at {db_path}")
    print(f"  Departments: {dept_count}")
    print(f"  Employees: {emp_count}")
    print(f"  Projects: {proj_count}")
    print(f"  Timesheets: {ts_count}")

    conn.close()


if __name__ == "__main__":
    db_path = Path(__file__).parent / "sample_data.db"
    create_database(db_path)
