import unittest
from app.core.parser import parse_source_file, _parse_python_ast, _parse_scoped_code

class AstParserTests(unittest.TestCase):
    def test_python_ast_parser_classes_and_methods(self):
        code = '''
class OrderService(BaseService):
    """Handles order processing."""
    
    @transactional
    def create_order(self, user_id: str, amount: float):
        """Creates a new user order."""
        validate_user(user_id)
        return {"status": "ok"}

def standalone_helper():
    return 42
'''
        parsed_dict = _parse_python_ast("services/order.py", code)
        self.assertIsNotNone(parsed_dict)
        self.assertEqual(len(parsed_dict["classes"]), 1)
        cls = parsed_dict["classes"][0]
        self.assertEqual(cls["name"], "OrderService")
        self.assertIn("BaseService", cls["base_classes"])
        self.assertEqual(cls["docstring"], "Handles order processing.")

        self.assertEqual(len(parsed_dict["functions"]), 2)
        create_fn = next(f for f in parsed_dict["functions"] if f["name"] == "create_order")
        self.assertEqual(create_fn["parent_class"], "OrderService")
        self.assertIn("transactional", create_fn["decorators"])
        self.assertIn("validate_user", create_fn["calls"])
        self.assertEqual(create_fn["docstring"], "Creates a new user order.")

        helper_fn = next(f for f in parsed_dict["functions"] if f["name"] == "standalone_helper")
        self.assertIsNone(helper_fn["parent_class"])

    def test_python_ast_test_target_detection(self):
        code = '''
def test_create_order_success():
    res = create_order("u1", 100)
    assert res["status"] == "ok"
'''
        parsed_dict = _parse_python_ast("tests/test_order.py", code)
        self.assertIsNotNone(parsed_dict)
        self.assertEqual(len(parsed_dict["functions"]), 1)
        test_fn = parsed_dict["functions"][0]
        self.assertEqual(test_fn["name"], "test_create_order_success")
        self.assertEqual(test_fn["test_target"], "create_order")

    def test_scoped_csharp_parser(self):
        code = '''
using System;
using SpotMarket.Services;

namespace SpotMarket {
    public class OrderManager : IOrderManager {
        public void ProcessOrder(string id) {
            ValidateOrder(id);
            SaveToDatabase(id);
        }
    }
}
'''
        parsed_dict = _parse_scoped_code("OrderManager.cs", "csharp", code)
        self.assertEqual(len(parsed_dict["classes"]), 1)
        cls = parsed_dict["classes"][0]
        self.assertEqual(cls["name"], "OrderManager")
        self.assertIn("IOrderManager", cls["base_classes"])

        self.assertEqual(len(parsed_dict["functions"]), 1)
        fn = parsed_dict["functions"][0]
        self.assertEqual(fn["name"], "ProcessOrder")
        self.assertEqual(fn["parent_class"], "OrderManager")
        self.assertIn("ValidateOrder", fn["calls"])
        self.assertIn("SaveToDatabase", fn["calls"])

    def test_scoped_javascript_parser(self):
        code = '''
import { logger } from './logger';

export class UserController {
    getUser(id) {
        logger.info(id);
        return findUser(id);
    }
}
'''
        parsed_dict = _parse_scoped_code("controller.js", "javascript", code)
        self.assertEqual(len(parsed_dict["classes"]), 1)
        cls = parsed_dict["classes"][0]
        self.assertEqual(cls["name"], "UserController")

        self.assertEqual(len(parsed_dict["functions"]), 1)
        fn = parsed_dict["functions"][0]
        self.assertEqual(fn["name"], "getUser")
        self.assertEqual(fn["parent_class"], "UserController")
        self.assertIn("findUser", fn["calls"])


if __name__ == '__main__':
    unittest.main()
