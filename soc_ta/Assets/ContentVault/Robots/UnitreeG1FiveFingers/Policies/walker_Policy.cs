using System;
using Hazel;

namespace walker_Policy
{
	public class walker_Policy : Entity
	{
		// Match this to the PolicySlot.Id you see for 'walker' in the RobotControllerComponent inspector.
		private const uint k_SlotId = 1;

		protected override void OnCreate()
		{
			RobotControllerComponent robot = GetComponent<RobotControllerComponent>();
			robot.SetPolicyActive(k_SlotId, true);
		}

		protected override void OnUpdate(float deltaTime)
		{
			RobotControllerComponent robot = GetComponent<RobotControllerComponent>();

			// --- Policy Commands (1-based ids, declaration order in descriptor) ---
			robot.SetFloat(k_SlotId, 1u, 0.0f);  // SetVx
			robot.SetFloat(k_SlotId, 2u, 0.0f);  // SetVy
			robot.SetFloat(k_SlotId, 3u, 0.0f);  // SetYawRate
		}
	}
}
