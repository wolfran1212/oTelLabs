output "ecs_cluster_name" {
  description = "Nombre del clúster ECS"
  value       = aws_ecs_cluster.main.name
}

output "ecs_cluster_arn" {
  description = "ARN del clúster ECS"
  value       = aws_ecs_cluster.main.arn
}

output "task_execution_role_arn" {
  description = "ARN del rol de ejecución ECS"
  value       = aws_iam_role.ecs_execution_role.arn
}

output "task_role_arn" {
  description = "ARN del rol de tarea con permisos X-Ray y CloudWatch"
  value       = aws_iam_role.ecs_task_role.arn
}

output "security_group_id" {
  description = "ID del Security Group para la tarea ECS"
  value       = aws_security_group.ecs_sg.id
}

output "public_subnets" {
  description = "Subredes públicas para la tarea Fargate"
  value       = [aws_subnet.public_a.id, aws_subnet.public_b.id]
}
